"""Access semantics: role expansion, valid sources, held and effective access (SPEC §5, §6.3)."""

from __future__ import annotations

import pytest

from identitybench.policy.access import (
    Grant,
    OracleInputError,
    RecordIndex,
    account_for,
    assignments_of,
    control_for,
    control_valid,
    effective_access,
    expand_role,
    held_access,
    item_risk_tier,
    opposite_side,
    pack_sides,
    review_source,
    role_grants,
    role_paths,
    source_grants,
    source_risk_tier,
    valid_assignments,
    valid_memberships,
)
from identitybench.schemas.case import ReviewItem, ReviewItemKind
from identitybench.world.entities import PolicyPack, RiskTier, SodRuleSides
from tests.policy.builders import AS_OF, CaseBuilder, day


def _nested(b: CaseBuilder) -> None:
    """role_a > role_b > role_c > role_d (depth 3), plus a diamond: role_a > role_e > role_d."""
    b.role("role_d", entitlements=["ent_deep"])
    b.role("role_c", entitlements=["ent_c"], child_roles=["role_d"])
    b.role("role_b", child_roles=["role_c"])
    b.role("role_e", child_roles=["role_d"])
    b.role("role_a", entitlements=["ent_a"], child_roles=["role_b", "role_e"])


def test_expand_role_follows_nesting_at_any_depth(rev: CaseBuilder) -> None:
    _nested(rev)
    assert expand_role(rev.roles, "role_a") == {"ent_a", "ent_c", "ent_deep"}
    assert expand_role(rev.roles, "role_d") == {"ent_deep"}


def test_expand_role_and_paths_survive_cycles(rev: CaseBuilder) -> None:
    rev.role("role_x", entitlements=["ent_x"], child_roles=["role_y"])
    rev.role("role_y", entitlements=["ent_y"], child_roles=["role_x"])
    assert expand_role(rev.roles, "role_x") == {"ent_x", "ent_y"}
    assert role_grants(rev.roles, "role_x") == (
        ("ent_x", ("role_x",)),
        ("ent_y", ("role_x", "role_y")),
    )


def test_role_paths_lists_every_alternative(rev: CaseBuilder) -> None:
    _nested(rev)
    assert role_paths(rev.roles, "role_a", "ent_deep") == (
        ("role_a", "role_b", "role_c", "role_d"),
        ("role_a", "role_e", "role_d"),
    )
    assert role_paths(rev.roles, "role_a", "ent_a") == (("role_a",),)
    assert role_paths(rev.roles, "role_a", "ent_missing") == ()


def test_missing_role_is_an_input_error(rev: CaseBuilder) -> None:
    rev.role("role_a", child_roles=["role_ghost"])
    with pytest.raises(OracleInputError, match="role_ghost"):
        expand_role(rev.roles, "role_a")
    with pytest.raises(OracleInputError, match="role_ghost"):
        role_grants(rev.roles, "role_a")


def test_validity_boundaries_for_sources(rev: CaseBuilder) -> None:
    rev.role("role_a", entitlements=["ent_a"])
    rev.membership("rm_today", "u_ben", "role_a", expires_on=AS_OF)
    rev.membership("rm_yesterday", "u_ben", "role_a", expires_on=day(-1))
    rev.membership("rm_tomorrow", "u_ben", "role_a", granted_on=day(1))
    rev.membership("rm_started_today", "u_ben", "role_a", granted_on=AS_OF)
    view = rev.campaign()
    valid = {m.id for m in valid_memberships(view, "u_ben", AS_OF)}
    assert valid == {"rm_today", "rm_started_today"}


def test_held_access_ignores_account_status_and_expired_sources(rev: CaseBuilder) -> None:
    rev.entitlement("ent_a")
    rev.entitlement("ent_b")
    rev.account("acct_disabled", "app_main", "u_ben", status="disabled")
    rev.assignment("as_on_disabled", "acct_disabled", "ent_a")
    rev.assignment("as_expired", "acct_ben_main", "ent_b", expires_on=day(-1))
    rev.account("acct_orphan", "app_main", None)
    rev.assignment("as_orphan", "acct_orphan", "ent_b")
    rev.identity("u_other")
    rev.account("acct_other", "app_main", "u_other")
    rev.assignment("as_other", "acct_other", "ent_b")
    view = rev.campaign()
    held = held_access(view, "u_ben", AS_OF)
    assert held == {"ent_a": (Grant("ent_a", "as_on_disabled", "assignment"),)}
    assert {a.id for a in assignments_of(view, "u_ben")} == {"as_on_disabled", "as_expired"}
    assert {a.id for a in valid_assignments(view, "u_ben", AS_OF)} == {"as_on_disabled"}


def test_assignment_on_a_missing_account_is_an_input_error(rev: CaseBuilder) -> None:
    """Whose access a dangling assignment is can't be known, so the view is rejected instead of
    the assignment being dropped from everyone's held access (SPEC §9.2)."""
    rev.entitlement("ent_a")
    rev.assignment("as_a", "acct_ben_main", "ent_a")
    rev.assignment("as_lost", "acct_gone", "ent_a", expires_on=day(-30))
    view = rev.campaign()
    with pytest.raises(OracleInputError, match=r"as_lost \(account acct_gone\)"):
        held_access(view, "u_ben", AS_OF)
    with pytest.raises(OracleInputError, match="acct_gone"):
        assignments_of(view, "u_mgr")
    with pytest.raises(OracleInputError, match="acct_gone"):
        RecordIndex(view).check_assignment_accounts()
    assert control_for(view, "u_ben", "SOD-002") is None  # lookups that ignore assignments work


def test_record_index_groups_records_by_lookup_key(rev: CaseBuilder) -> None:
    rev.entitlement("ent_a")
    rev.account("acct_ben_b", "app_main", "u_ben")
    rev.use("use_2", "acct_ben_main", "ent_a", day(-1))
    rev.use("use_1", "acct_ben_main", "ent_a", day(-2))
    rev.control("mc_1", "SOD-002", "u_ben")
    records = RecordIndex(rev.campaign())
    assert [a.id for a in records.accounts_of("u_ben")] == ["acct_ben_b", "acct_ben_main"]
    assert records.accounts_of("u_nobody") == ()
    assert [u.id for u in records.usage_for("acct_ben_main", "ent_a")] == ["use_1", "use_2"]
    assert records.usage_for("acct_ben_b", "ent_a") == ()
    assert records.control_for("u_ben", "SOD-002") == rev.controls["mc_1"]
    assert records.control_for("u_mgr", "SOD-002") is None


def test_held_access_groups_every_source_and_path(rev: CaseBuilder) -> None:
    _nested(rev)
    rev.entitlement("ent_deep")
    rev.membership("rm_a", "u_ben", "role_a")
    rev.assignment("as_deep", "acct_ben_main", "ent_deep")
    held = held_access(rev.campaign(), "u_ben", AS_OF)
    assert held["ent_deep"] == (
        Grant("ent_deep", "as_deep", "assignment"),
        Grant("ent_deep", "rm_a", "membership", ("role_a", "role_b", "role_c", "role_d")),
        Grant("ent_deep", "rm_a", "membership", ("role_a", "role_e", "role_d")),
    )
    assert list(held) == sorted(held)


def test_effective_access_needs_an_active_account(rev: CaseBuilder) -> None:
    rev.app("app_other")
    rev.entitlement("ent_main_role")
    rev.entitlement("ent_other_role", app_id="app_other")
    rev.entitlement("ent_direct")
    rev.entitlement("ent_suspended", app_id="app_other")
    rev.role("role_mix", entitlements=["ent_main_role", "ent_other_role"])
    rev.membership("rm_mix", "u_ben", "role_mix")
    rev.assignment("as_direct", "acct_ben_main", "ent_direct")
    rev.account(
        "acct_ben_other", "app_other", "u_ben", status="suspended", suspension_reason="leave"
    )
    rev.assignment("as_suspended", "acct_ben_other", "ent_suspended")
    view = rev.campaign()
    held = held_access(view, "u_ben", AS_OF)
    effective = effective_access(view, "u_ben", AS_OF)
    assert set(held) == {"ent_main_role", "ent_other_role", "ent_direct", "ent_suspended"}
    assert set(effective) == {"ent_main_role", "ent_direct"}


def test_account_for_prefers_the_most_permissive_human_account(rev: CaseBuilder) -> None:
    rev.app("app_other")
    rev.account("acct_b_disabled", "app_other", "u_ben", status="disabled")
    rev.account("acct_a_active", "app_other", "u_ben")
    rev.account("acct_service", "app_other", None, kind="service", owner_id="u_ben")
    view = rev.campaign()
    assert account_for(view, "u_ben", "app_main") == rev.accounts["acct_ben_main"]
    assert account_for(view, "u_ben", "app_other") == rev.accounts["acct_a_active"]
    assert account_for(view, "u_mgr", "app_main") is None


def test_risk_tier_of_items(rev: CaseBuilder) -> None:
    rev.entitlement("ent_low")
    rev.entitlement("ent_high", risk_tier="high")
    rev.entitlement("ent_crit", risk_tier="critical")
    rev.role("role_child", entitlements=["ent_crit"])
    rev.role("role_parent", entitlements=["ent_low"], child_roles=["role_child"])
    rev.role("role_empty")
    rev.membership("rm_parent", "u_ben", "role_parent")
    rev.membership("rm_empty", "u_ben", "role_empty")
    rev.assignment("as_high", "acct_ben_main", "ent_high")
    view = rev.campaign("rm_parent", "rm_empty", "as_high")
    assert source_risk_tier(view, rev.memberships["rm_parent"]) == RiskTier.critical
    assert source_risk_tier(view, rev.memberships["rm_empty"]) == RiskTier.low
    assert source_risk_tier(view, rev.assignments["as_high"]) == RiskTier.high
    assert view.campaign is not None
    tiers = {i.item_id: item_risk_tier(view, i) for i in view.campaign.items}
    assert tiers == {"rv_rm_parent": "critical", "rv_rm_empty": "low", "rv_as_high": "high"}


def test_risk_tier_needs_every_entitlement(rev: CaseBuilder) -> None:
    rev.role("role_a", entitlements=["ent_ghost"])
    rev.membership("rm_a", "u_ben", "role_a")
    with pytest.raises(OracleInputError, match="ent_ghost"):
        source_risk_tier(rev.campaign(), rev.memberships["rm_a"])


def test_review_source_rejects_unknown_refs(rev: CaseBuilder) -> None:
    view = rev.campaign()
    with pytest.raises(OracleInputError, match="rm_nope"):
        review_source(
            view, ReviewItem(item_id="rv_1", kind=ReviewItemKind.membership, ref="rm_nope")
        )
    with pytest.raises(OracleInputError, match="as_nope"):
        review_source(
            view, ReviewItem(item_id="rv_2", kind=ReviewItemKind.assignment, ref="as_nope")
        )


def test_source_grants_for_each_kind(rev: CaseBuilder) -> None:
    rev.role("role_a", entitlements=["ent_a"])
    membership = rev.membership("rm_a", "u_ben", "role_a")
    assignment = rev.assignment("as_x", "acct_ben_main", "ent_x")
    assert source_grants(rev.roles, membership) == (
        Grant("ent_a", "rm_a", "membership", ("role_a",)),
    )
    assert source_grants(rev.roles, assignment) == (Grant("ent_x", "as_x", "assignment"),)


def test_pack_sides_validates_and_orders() -> None:
    def sides(rule_id: str) -> SodRuleSides:
        return SodRuleSides(rule_id=rule_id, title=rule_id, side_a=("a",), side_b=("b",))

    pack = PolicyPack(org_name="x", sod=(sides("SOD-005"), sides("SOD-002")))
    assert list(pack_sides(pack)) == ["SOD-002", "SOD-005"]
    with pytest.raises(OracleInputError, match="more than once"):
        pack_sides(PolicyPack(org_name="x", sod=(sides("SOD-002"), sides("SOD-002"))))
    with pytest.raises(OracleInputError, match="unknown SoD rule"):
        pack_sides(PolicyPack(org_name="x", sod=(sides("SOD-009"),)))


def test_opposite_side() -> None:
    rule = SodRuleSides(rule_id="SOD-001", title="t", side_a=("a1", "a2"), side_b=("b1",))
    assert opposite_side(rule, "a2") == {"b1"}
    assert opposite_side(rule, "b1") == {"a1", "a2"}
    assert opposite_side(rule, "zzz") == frozenset()
    both = SodRuleSides(rule_id="SOD-001", title="t", side_a=("x", "a"), side_b=("x", "b"))
    assert opposite_side(both, "x") == {"x", "a", "b"}


def test_control_lookup_and_validity(rev: CaseBuilder) -> None:
    control = rev.control("mc_1", "SOD-002", "u_ben", granted_on=AS_OF, expires_on=AS_OF)
    view = rev.campaign()
    assert control_for(view, "u_ben", "SOD-002") == control
    assert control_for(view, "u_ben", "SOD-001") is None
    assert control_valid(control, AS_OF)
    assert not control_valid(control, day(1))
    assert not control_valid(control, day(-1))
    rev.control("mc_2", "SOD-002", "u_ben")
    with pytest.raises(OracleInputError, match="several controls"):
        control_for(rev.campaign(), "u_ben", "SOD-002")
