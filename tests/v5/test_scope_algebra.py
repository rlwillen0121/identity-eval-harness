from __future__ import annotations

from identitybench.v5.schemas import (
    CompensatingControlV5,
    EngagementV5,
    GrantV5,
    GroupV5,
    HRPersonV5,
    IdPAccountV5,
    RoleV5,
    ScopeCatalogV5,
    ScopedSoDRuleV5,
)
from identitybench.v5.scope_algebra import (
    ScopeAlgebraV5,
    capability_footprint,
    effective_department,
    effective_grant_footprint,
    engagement_is_live,
    footprint_join,
    footprint_leq,
    footprint_meet,
    group_closure,
    group_footprints,
    has_live_engagement,
    is_subordinate,
    normalize_footprint,
    scope_leq,
    scope_meet,
    sod_witnesses,
    syntactically_mitigates,
    unmitigated_witnesses,
)


def _account(*group_ids: str) -> IdPAccountV5:
    return IdPAccountV5(
        account_id="acct_person_a",
        subkind="human_primary",
        login_email="a@example.com",
        status="ACTIVE",
        group_ids=group_ids,
        etag="0123456789abcdef",
        evidence_id="ev_account",
    )


def _engagement(**updates: object) -> EngagementV5:
    values: dict[str, object] = {
        "eng_id": "eng_a",
        "affiliation": "FTE",
        "eng_status": "ACTIVE",
        "dept_path": "/org/eng/platform",
        "start_s": 10,
    }
    values.update(updates)
    return EngagementV5.model_validate(values)


def _person(person_id: str, *engagements: EngagementV5) -> HRPersonV5:
    return HRPersonV5(
        person_id=person_id,
        emp_immutable_id=f"emp_{person_id}",
        primary_email=f"{person_id}@example.com",
        engagements=engagements,
        etag="0123456789abcdef",
        evidence_id=f"ev_{person_id}",
    )


def test_scope_resolution_order_and_footprint_lattice() -> None:
    catalog = ScopeCatalogV5(
        leaf_scopes=(
            "/org/eng/platform",
            "/org/eng/security",
            "/org/finance",
        )
    )

    assert catalog.leaves("/*") == frozenset(catalog.leaf_scopes)
    assert catalog.leaves("/org/eng") == frozenset({"/org/eng/platform", "/org/eng/security"})
    assert scope_leq(catalog, "/org/eng/platform", "/org/eng")
    assert not scope_leq(catalog, "/org/eng", "/org/eng/platform")
    assert scope_meet(catalog, "/org/eng", "/org/eng/security") == frozenset({"/org/eng/security"})
    assert scope_meet(catalog) == frozenset(catalog.leaf_scopes)

    algebra = ScopeAlgebraV5(catalog)
    eng = algebra.capability_footprint({"read", "write"}, ("/org/eng",))
    platform = capability_footprint(catalog, ("read",), ("/org/eng/platform",))
    assert footprint_leq(platform, eng)
    assert footprint_meet(eng, platform) == platform
    assert footprint_join(platform, frozenset({("admin", "/org/finance")})) == frozenset(
        {("read", "/org/eng/platform"), ("admin", "/org/finance")}
    )
    assert normalize_footprint(platform, catalog) == platform
    try:
        normalize_footprint({("read", "/unknown")}, catalog)
    except ValueError as exc:
        assert "Unknown leaf" in str(exc)
    else:
        raise AssertionError("unknown finite leaf was accepted")


def test_cycle_safe_group_closure_and_deny_masks_group_and_grant_allow() -> None:
    catalog = ScopeCatalogV5(leaf_scopes=("/org/eng/platform", "/org/eng/security", "/org/finance"))
    allow_group = GroupV5(
        group_id="grp_allow",
        member_of_group_ids=("grp_deny",),
        effect="ALLOW",
        scope_path="/org/eng",
        capabilities=("read", "write"),
        granted_role_ids=("role_admin",),
        evidence_id="ev_allow",
    )
    deny_group = GroupV5(
        group_id="grp_deny",
        member_of_group_ids=("grp_allow",),
        effect="DENY",
        scope_path="/org/eng/security",
        capabilities=("read",),
        evidence_id="ev_deny",
    )
    role = RoleV5(
        role_id="role_admin",
        app_id="app_iga",
        privilege_tier="admin",
        capabilities=("admin",),
        evidence_id="ev_role",
    )
    groups = {allow_group.group_id: allow_group, deny_group.group_id: deny_group}
    roles = {role.role_id: role}
    account = _account("grp_allow", "grp_missing")

    assert group_closure(account, groups) == frozenset({"grp_allow", "grp_deny"})
    group_allow, group_deny = group_footprints(account, groups, roles, catalog)
    assert ("read", "/org/eng/platform") in group_allow
    assert ("read", "/org/eng/security") not in group_allow
    assert ("write", "/org/eng/security") in group_allow
    assert ("admin", "/org/eng/security") in group_allow
    assert group_deny == frozenset({("read", "/org/eng/security")})

    grant_role = RoleV5(
        role_id="role_reader",
        app_id="app_iga",
        privilege_tier="standard",
        capabilities=("read", "write"),
        evidence_id="ev_reader",
    )
    grant = GrantV5(
        grant_id="grant_a",
        account_id=account.account_id,
        role_id=grant_role.role_id,
        grant_mode="standing",
        scope_path="/org/eng",
        sod_prune_priority=1,
        evidence_id="ev_grant",
    )
    effective = effective_grant_footprint(
        grant, {grant_role.role_id: grant_role}, group_deny, catalog
    )
    assert ("read", "/org/eng/platform") in effective
    assert ("read", "/org/eng/security") not in effective
    assert ("write", "/org/eng/security") in effective


def test_sod_witnesses_are_symmetric_and_global_control_needs_full_catalog() -> None:
    catalog = ScopeCatalogV5(leaf_scopes=("/org/eng/platform", "/org/eng/security", "/org/finance"))
    first = frozenset({("read", "/org/eng/platform")})
    second = frozenset({("write", "/org/eng/security")})
    same_leaf = ScopedSoDRuleV5(
        rule_code="SOD-SAME",
        cap_a="read",
        cap_b="write",
        scope_mode="SAME_LEAF_SCOPE",
    )
    global_rule = ScopedSoDRuleV5(
        rule_code="SOD-GLOBAL",
        cap_a="write",
        cap_b="read",
        scope_mode="GLOBAL",
    )

    assert sod_witnesses(first, second, (same_leaf,)) == frozenset()
    witnesses = sod_witnesses(first, second, (global_rule,))
    assert witnesses == sod_witnesses(second, first, (global_rule,))
    witness = next(iter(witnesses))
    partial_control = CompensatingControlV5(
        cc_id="cc_partial",
        monitored_person_id="pers_a",
        cap_a="read",
        cap_b="write",
        covered_scope_path="/org/eng",
        monitor_account_id="acct_monitor",
        required_monitor_capability="audit",
        audit_connector_id="conn_a",
        etag="0123456789abcdef",
        evidence_id="ev_cc_partial",
    )
    full_control = partial_control.model_copy(
        update={"cc_id": "cc_full", "covered_scope_path": "/*"}
    )

    assert not syntactically_mitigates(partial_control, "pers_a", witness, global_rule, catalog)
    assert syntactically_mitigates(full_control, "pers_a", witness, global_rule, catalog)
    assert not syntactically_mitigates(full_control, "pers_b", witness, global_rule, catalog)
    assert (
        unmitigated_witnesses(first, second, "pers_a", (full_control,), (global_rule,), catalog)
        == frozenset()
    )
    assert unmitigated_witnesses(first, second, "pers_a", (), (global_rule,), catalog) == witnesses

    # Rule codes are agent-visible witness IDs but the schema does not require
    # uniqueness. A same-leaf duplicate must not shadow the GLOBAL rule.
    duplicate_code = global_rule.model_copy(update={"scope_mode": "SAME_LEAF_SCOPE"})
    assert sod_witnesses(first, second, (global_rule, duplicate_code)) == witnesses
    assert (
        unmitigated_witnesses(
            first, second, "pers_a", (partial_control,), (global_rule, duplicate_code), catalog
        )
        == witnesses
    )


def test_engagement_liveness_department_and_live_manager_walk() -> None:
    active = _engagement(
        end_s=40,
        retro_terminated_effective_s=35,
        prior_dept_path="/org/eng/security",
        dept_transfer_effective_at_s=30,
    )
    pending = _engagement(
        eng_id="eng_pending",
        eng_status="CONTRACTOR_TO_FTE_PENDING",
        conversion_start_s=50,
        conversion_grace_deadline_s=80,
        retro_terminated_effective_s=75,
    )
    terminated = _engagement(eng_id="eng_term", eng_status="TERMINATED")

    assert not engagement_is_live(active, 9)
    assert engagement_is_live(active, 29)
    assert effective_department(active, 29) == "/org/eng/security"
    assert effective_department(active, 30) == "/org/eng/platform"
    assert not engagement_is_live(active, 35)
    # The normative pending predicate requires a non-null conversion start, but
    # deliberately gates only on the grace deadline and retro-termination.
    assert engagement_is_live(pending, 49)
    assert engagement_is_live(pending, 74)
    assert not engagement_is_live(pending, 75)
    assert not engagement_is_live(terminated, 20)

    person_c = _person("pers_c", _engagement(manager_person_id="pers_b", end_s=40))
    person_b = _person("pers_b", _engagement(manager_person_id="pers_a", end_s=40))
    person_a = _person("pers_a", _engagement(manager_person_id="pers_c", end_s=40))
    people = {p.person_id: p for p in (person_a, person_b, person_c)}
    assert has_live_engagement(person_c, 20)
    assert is_subordinate("pers_c", "pers_a", 20, people)
    assert not is_subordinate("pers_a", "pers_a", 20, people)
    assert not is_subordinate("pers_c", "pers_a", 45, people)
