use std::collections::{BTreeSet, HashMap};

use ib_v5_schemas::{
    ActiveDisabledStatusV5, AffiliationKindV5, CapabilityFootprint, CompensatingControlV5,
    EngagementStatusV5, EngagementV5, GrantModeV5, GrantV5, GroupEffectV5, GroupV5, HRPersonV5,
    HumanSubkindV5, IdPAccountV5, PrivilegeTierV5, RoleV5, ScopeAlgebraV5, ScopeCatalogV5,
    ScopedSoDRuleV5, SoDScopeModeV5, capability_footprint, effective_department,
    effective_grant_footprint, engagement_is_live, footprint_join, footprint_leq, footprint_meet,
    group_closure, group_footprints, has_live_engagement, is_subordinate, normalize_footprint,
    scope_leq, scope_meet, sod_witnesses, syntactically_mitigates, unmitigated_witnesses,
};

fn make_account(group_ids: &[&str]) -> IdPAccountV5 {
    IdPAccountV5 {
        account_id: "acct_person_a".to_owned(),
        subkind: HumanSubkindV5::HumanPrimary,
        login_email: "a@example.com".to_owned(),
        immutable_person_ref: None,
        bound_engagement_ref: None,
        status: ActiveDisabledStatusV5::Active,
        group_ids: group_ids.iter().map(|s| (*s).to_owned()).collect(),
        direct_grant_ids: vec![],
        account_gen: 1,
        etag: "0123456789abcdef".to_owned(),
        evidence_id: "ev_account".to_owned(),
    }
}

fn make_engagement(eng_id: &str, status: EngagementStatusV5) -> EngagementV5 {
    EngagementV5 {
        eng_id: eng_id.to_owned(),
        affiliation: AffiliationKindV5::Fte,
        eng_status: status,
        dept_path: "/org/eng/platform".to_owned(),
        prior_dept_path: None,
        dept_transfer_effective_at_s: None,
        manager_person_id: None,
        start_s: 10,
        end_s: None,
        conversion_start_s: None,
        conversion_grace_deadline_s: None,
        retro_terminated_effective_s: None,
    }
}

fn make_person(person_id: &str, engagements: Vec<EngagementV5>) -> HRPersonV5 {
    HRPersonV5 {
        person_id: person_id.to_owned(),
        emp_immutable_id: format!("emp_{person_id}"),
        primary_email: format!("{person_id}@example.com"),
        historical_emails: vec![],
        engagements,
        lifecycle_gen: 1,
        etag: "0123456789abcdef".to_owned(),
        evidence_id: format!("ev_{person_id}"),
    }
}

#[test]
fn scope_resolution_order_and_footprint_lattice() {
    let catalog = ScopeCatalogV5 {
        leaf_scopes: vec![
            "/org/eng/platform".to_owned(),
            "/org/eng/security".to_owned(),
            "/org/finance".to_owned(),
        ],
    };

    assert_eq!(
        catalog.leaves("/*").unwrap(),
        catalog.leaf_scopes.iter().cloned().collect::<BTreeSet<_>>()
    );
    assert_eq!(
        catalog.leaves("/org/eng").unwrap(),
        BTreeSet::from([
            "/org/eng/platform".to_owned(),
            "/org/eng/security".to_owned()
        ])
    );
    assert!(scope_leq(&catalog, "/org/eng/platform", "/org/eng").unwrap());
    assert!(!scope_leq(&catalog, "/org/eng", "/org/eng/platform").unwrap());
    assert_eq!(
        scope_meet(&catalog, &["/org/eng", "/org/eng/security"]).unwrap(),
        BTreeSet::from(["/org/eng/security".to_owned()])
    );
    assert_eq!(
        scope_meet(&catalog, &[]).unwrap(),
        catalog.leaf_scopes.iter().cloned().collect::<BTreeSet<_>>()
    );

    let algebra = ScopeAlgebraV5::new(catalog.clone());
    let eng = algebra
        .capability_footprint(["read", "write"], ["/org/eng"])
        .unwrap();
    let platform = capability_footprint(&catalog, ["read"], ["/org/eng/platform"]).unwrap();
    assert!(footprint_leq(&platform, &eng));
    assert_eq!(footprint_meet(&[&eng, &platform]), platform);

    let extra: CapabilityFootprint =
        BTreeSet::from([("admin".to_owned(), "/org/finance".to_owned())]);
    assert_eq!(
        footprint_join(&[&platform, &extra]),
        BTreeSet::from([
            ("read".to_owned(), "/org/eng/platform".to_owned()),
            ("admin".to_owned(), "/org/finance".to_owned()),
        ])
    );
    assert_eq!(
        normalize_footprint(
            platform.iter().map(|(c, l)| (c.as_str(), l.as_str())),
            &catalog
        )
        .unwrap(),
        platform
    );
    let err = normalize_footprint([("read", "/unknown")], &catalog).unwrap_err();
    assert!(err.to_string().contains("Unknown leaf"));
}

#[test]
fn cycle_safe_group_closure_and_deny_masks_group_and_grant_allow() {
    let catalog = ScopeCatalogV5 {
        leaf_scopes: vec![
            "/org/eng/platform".to_owned(),
            "/org/eng/security".to_owned(),
            "/org/finance".to_owned(),
        ],
    };
    let allow_group = GroupV5 {
        group_id: "grp_allow".to_owned(),
        member_of_group_ids: vec!["grp_deny".to_owned()],
        effect: GroupEffectV5::Allow,
        scope_path: "/org/eng".to_owned(),
        capabilities: vec!["read".to_owned(), "write".to_owned()],
        granted_role_ids: vec!["role_admin".to_owned()],
        evidence_id: "ev_allow".to_owned(),
    };
    let deny_group = GroupV5 {
        group_id: "grp_deny".to_owned(),
        member_of_group_ids: vec!["grp_allow".to_owned()],
        effect: GroupEffectV5::Deny,
        scope_path: "/org/eng/security".to_owned(),
        capabilities: vec!["read".to_owned()],
        granted_role_ids: vec![],
        evidence_id: "ev_deny".to_owned(),
    };
    let role = RoleV5 {
        role_id: "role_admin".to_owned(),
        app_id: "app_iga".to_owned(),
        privilege_tier: PrivilegeTierV5::Admin,
        capabilities: vec!["admin".to_owned()],
        is_jit_capable_target: true,
        evidence_id: "ev_role".to_owned(),
    };
    let groups = HashMap::from([
        (allow_group.group_id.clone(), allow_group),
        (deny_group.group_id.clone(), deny_group),
    ]);
    let roles = HashMap::from([(role.role_id.clone(), role)]);
    let account = make_account(&["grp_allow", "grp_missing"]);

    assert_eq!(
        group_closure(&account, &groups),
        BTreeSet::from(["grp_allow".to_owned(), "grp_deny".to_owned()])
    );
    let (group_allow, group_deny) = group_footprints(&account, &groups, &roles, &catalog).unwrap();
    assert!(group_allow.contains(&("read".to_owned(), "/org/eng/platform".to_owned())));
    assert!(!group_allow.contains(&("read".to_owned(), "/org/eng/security".to_owned())));
    assert!(group_allow.contains(&("write".to_owned(), "/org/eng/security".to_owned())));
    assert!(group_allow.contains(&("admin".to_owned(), "/org/eng/security".to_owned())));
    assert_eq!(
        group_deny,
        BTreeSet::from([("read".to_owned(), "/org/eng/security".to_owned())])
    );

    let grant_role = RoleV5 {
        role_id: "role_reader".to_owned(),
        app_id: "app_iga".to_owned(),
        privilege_tier: PrivilegeTierV5::Standard,
        capabilities: vec!["read".to_owned(), "write".to_owned()],
        is_jit_capable_target: true,
        evidence_id: "ev_reader".to_owned(),
    };
    let grant = GrantV5 {
        grant_id: "grant_a".to_owned(),
        account_id: account.account_id.clone(),
        role_id: grant_role.role_id.clone(),
        grant_mode: GrantModeV5::Standing,
        scope_path: "/org/eng".to_owned(),
        sod_prune_priority: 1,
        evidence_id: "ev_grant".to_owned(),
    };
    let roles2 = HashMap::from([(grant_role.role_id.clone(), grant_role)]);
    let effective = effective_grant_footprint(&grant, &roles2, &group_deny, &catalog).unwrap();
    assert!(effective.contains(&("read".to_owned(), "/org/eng/platform".to_owned())));
    assert!(!effective.contains(&("read".to_owned(), "/org/eng/security".to_owned())));
    assert!(effective.contains(&("write".to_owned(), "/org/eng/security".to_owned())));
}

#[test]
fn sod_witnesses_are_symmetric_and_global_control_needs_full_catalog() {
    let catalog = ScopeCatalogV5 {
        leaf_scopes: vec![
            "/org/eng/platform".to_owned(),
            "/org/eng/security".to_owned(),
            "/org/finance".to_owned(),
        ],
    };
    let first: CapabilityFootprint =
        BTreeSet::from([("read".to_owned(), "/org/eng/platform".to_owned())]);
    let second: CapabilityFootprint =
        BTreeSet::from([("write".to_owned(), "/org/eng/security".to_owned())]);
    let same_leaf = ScopedSoDRuleV5 {
        rule_code: "SOD-SAME".to_owned(),
        cap_a: "read".to_owned(),
        cap_b: "write".to_owned(),
        scope_mode: SoDScopeModeV5::SameLeafScope,
    };
    let global_rule = ScopedSoDRuleV5 {
        rule_code: "SOD-GLOBAL".to_owned(),
        cap_a: "write".to_owned(),
        cap_b: "read".to_owned(),
        scope_mode: SoDScopeModeV5::Global,
    };

    assert!(sod_witnesses(&first, &second, [&same_leaf]).is_empty());
    let witnesses = sod_witnesses(&first, &second, [&global_rule]);
    assert_eq!(witnesses, sod_witnesses(&second, &first, [&global_rule]));
    let witness = witnesses.iter().next().unwrap();

    let partial_control = CompensatingControlV5 {
        cc_id: "cc_partial".to_owned(),
        monitored_person_id: "pers_a".to_owned(),
        cap_a: "read".to_owned(),
        cap_b: "write".to_owned(),
        covered_scope_path: "/org/eng".to_owned(),
        monitor_account_id: "acct_monitor".to_owned(),
        required_monitor_capability: "audit".to_owned(),
        audit_connector_id: "conn_a".to_owned(),
        preserve_if_live: true,
        etag: "0123456789abcdef".to_owned(),
        evidence_id: "ev_cc_partial".to_owned(),
    };
    let mut full_control = partial_control.clone();
    full_control.cc_id = "cc_full".to_owned();
    full_control.covered_scope_path = "/*".to_owned();

    assert!(
        !syntactically_mitigates(&partial_control, "pers_a", witness, &global_rule, &catalog)
            .unwrap()
    );
    assert!(
        syntactically_mitigates(&full_control, "pers_a", witness, &global_rule, &catalog).unwrap()
    );
    assert!(
        !syntactically_mitigates(&full_control, "pers_b", witness, &global_rule, &catalog).unwrap()
    );
    assert!(
        unmitigated_witnesses(
            &first,
            &second,
            "pers_a",
            [&full_control],
            [&global_rule],
            &catalog,
        )
        .unwrap()
        .is_empty()
    );
    assert_eq!(
        unmitigated_witnesses(&first, &second, "pers_a", [], [&global_rule], &catalog).unwrap(),
        witnesses
    );

    let mut duplicate_code = global_rule.clone();
    duplicate_code.scope_mode = SoDScopeModeV5::SameLeafScope;
    assert_eq!(
        sod_witnesses(&first, &second, [&global_rule, &duplicate_code]),
        witnesses
    );
    assert_eq!(
        unmitigated_witnesses(
            &first,
            &second,
            "pers_a",
            [&partial_control],
            [&global_rule, &duplicate_code],
            &catalog,
        )
        .unwrap(),
        witnesses
    );
}

#[test]
fn engagement_liveness_department_and_live_manager_walk() {
    let mut active = make_engagement("eng_a", EngagementStatusV5::Active);
    active.end_s = Some(40);
    active.retro_terminated_effective_s = Some(35);
    active.prior_dept_path = Some("/org/eng/security".to_owned());
    active.dept_transfer_effective_at_s = Some(30);

    let mut pending = make_engagement("eng_pending", EngagementStatusV5::ContractorToFtePending);
    pending.conversion_start_s = Some(50);
    pending.conversion_grace_deadline_s = Some(80);
    pending.retro_terminated_effective_s = Some(75);

    let terminated = make_engagement("eng_term", EngagementStatusV5::Terminated);

    assert!(!engagement_is_live(&active, 9));
    assert!(engagement_is_live(&active, 29));
    assert_eq!(effective_department(&active, 29), "/org/eng/security");
    assert_eq!(effective_department(&active, 30), "/org/eng/platform");
    assert!(!engagement_is_live(&active, 35));
    assert!(engagement_is_live(&pending, 49));
    assert!(engagement_is_live(&pending, 74));
    assert!(!engagement_is_live(&pending, 75));
    assert!(!engagement_is_live(&terminated, 20));

    let mut ec = make_engagement("eng_c", EngagementStatusV5::Active);
    ec.manager_person_id = Some("pers_b".to_owned());
    ec.end_s = Some(40);
    let mut eb = make_engagement("eng_b", EngagementStatusV5::Active);
    eb.manager_person_id = Some("pers_a".to_owned());
    eb.end_s = Some(40);
    let mut ea = make_engagement("eng_a", EngagementStatusV5::Active);
    ea.manager_person_id = Some("pers_c".to_owned());
    ea.end_s = Some(40);

    let pc = make_person("pers_c", vec![ec]);
    let pb = make_person("pers_b", vec![eb]);
    let pa = make_person("pers_a", vec![ea]);
    let people = HashMap::from([
        (pa.person_id.clone(), pa),
        (pb.person_id.clone(), pb),
        (pc.person_id.clone(), pc),
    ]);

    assert!(has_live_engagement(people.get("pers_c").unwrap(), 20));
    assert!(is_subordinate("pers_c", "pers_a", 20, &people));
    assert!(!is_subordinate("pers_a", "pers_a", 20, &people));
    assert!(!is_subordinate("pers_c", "pers_a", 45, &people));
}
