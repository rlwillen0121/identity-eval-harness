"""Surface features: the pinned feature list, no rule inputs, and correct values."""

from __future__ import annotations

import json
from typing import Any

import pytest

from identitybench.policy.access import OracleInputError
from identitybench.probe.features import (
    FAMILY_PREFIX,
    NON_FEATURE_KEYS,
    REQUEST_FEATURES,
    REVIEW_ITEM_FEATURES,
    TEXT_FAMILY_KEYS,
    VIEW_FEATURES,
    family_features,
    is_family_feature,
    request_features,
    review_item_features,
    view_features,
)
from identitybench.schemas.case import (
    Campaign,
    CampaignScopeKind,
    Case,
    CaseView,
    ReviewItem,
    ReviewItemKind,
)
from identitybench.schemas.enums import Label, ReviewDecision, Split, Task, Tier
from identitybench.schemas.gold import ReviewGold, ReviewItemGold
from identitybench.world.entities import (
    AccountKind,
    AccountStatus,
    DataClassification,
    EmploymentType,
    Environment,
    IdentityStatus,
    Location,
    PolicyPack,
    Privilege,
    RiskTier,
    RoleKind,
    SodRuleSides,
    TelemetryStatus,
    TicketStatus,
    TicketType,
)
from tests.probe._cases import (
    ACCOUNT_OWNERSHIPS,
    ItemSpec,
    account,
    account_held,
    add_entities,
    application,
    assignment,
    control,
    day,
    entitlement,
    identity,
    jml_case,
    make_case,
    make_view,
    membership,
    ownership_campaign,
    request_case,
    review_case,
    role,
    ticket,
    usage,
    world_cases_with_variants,
)

EXPECTED_VIEW = (
    "view_identities",
    "view_applications",
    "view_entitlements",
    "view_roles",
    "view_role_child_links",
    "view_role_entitlement_links",
    "view_memberships",
    "view_accounts",
    "view_assignments",
    "view_usage_records",
    "view_controls",
    "view_tickets",
    "view_history_records",
    "view_peer_stat_identities",
    "has_controls",
    "has_tickets",
    "ticket_summary_chars",
    "entitlement_description_chars_total",
)
EXPECTED_REQUEST = (
    *EXPECTED_VIEW,
    "justification_chars",
    "justification_words",
    "has_ticket_ref",
    "is_break_glass",
    "on_behalf",
    "has_requested_duration",
    "entitlement_description_chars",
)
EXPECTED_REVIEW = (
    *EXPECTED_VIEW,
    "campaign_items",
    "campaign_membership_items",
    "campaign_assignment_items",
    "campaign_scope_manager",
    "campaign_has_notes",
    "campaign_notes_chars",
    "campaign_items_with_note",
    "item_is_membership",
    "item_position",
    "item_position_frac",
    "item_has_note",
    "item_note_chars",
    "item_assignment_description_chars",
)

FORBIDDEN_TOKENS = (
    "employment",
    "status",
    "privilege",
    "risk",
    "tier",
    "sod",
    "conflict",
    "date",
    "expire",
    "granted",
    "last_used",
    "dormant",
    "source",
    "classification",
    "requestable",
    "eligible",
    "region",
    "environment",
    "gold",
    "label",
    "decision",
    "scenario",
    "twin",
    "variant",
    "peer_share",
    "owner",
    "orphan",
    "holder",
    "service",
)
"""Name fragments of rule inputs and labels that no surface feature may carry. (Values can
leak too, which names can't show: see ``test_review_features_ignore_account_ownership``.)"""


def _replace(case: Case, **view_changes: Any) -> Case:
    return case.model_copy(update={"view": case.view.model_copy(update=view_changes)})


def test_feature_lists_are_pinned() -> None:
    assert VIEW_FEATURES == EXPECTED_VIEW
    assert REQUEST_FEATURES == EXPECTED_REQUEST
    assert REVIEW_ITEM_FEATURES == EXPECTED_REVIEW
    assert len(set(REQUEST_FEATURES)) == len(REQUEST_FEATURES)
    assert len(set(REVIEW_ITEM_FEATURES)) == len(REVIEW_ITEM_FEATURES)


@pytest.mark.parametrize("name", sorted({*REQUEST_FEATURES, *REVIEW_ITEM_FEATURES}))
def test_no_feature_names_a_rule_input(name: str) -> None:
    assert not any(token in name for token in FORBIDDEN_TOKENS), name


def test_feature_rows_have_exactly_the_listed_keys() -> None:
    assert tuple(request_features(request_case("c1"))) == REQUEST_FEATURES
    items = [ItemSpec(), ItemSpec(ReviewItemKind.membership)]
    for row in review_item_features(review_case("c2", items)):
        assert tuple(row) == REVIEW_ITEM_FEATURES


def test_request_feature_values() -> None:
    case = request_case(
        "c1",
        justification="Close the books now please",
        ticket_ref="INC-1",
        is_break_glass=True,
        requester_id="u_mgr",
        duration=None,
        peer_share=0.4,
        extra=[ticket("INC-1", summary="abcde"), control("mc_1", "u_ben")],
    )
    row = request_features(case)
    assert row["justification_chars"] == len("Close the books now please")
    assert row["justification_words"] == 5
    assert row["has_ticket_ref"] == 1.0
    assert row["is_break_glass"] == 1.0
    assert row["on_behalf"] == 1.0
    assert row["has_requested_duration"] == 0.0
    assert row["entitlement_description_chars"] == len("Grants read access to reports.")
    assert row["view_identities"] == 2.0
    assert row["view_tickets"] == 1.0
    assert row["has_tickets"] == 1.0
    assert row["ticket_summary_chars"] == 5.0
    assert row["view_controls"] == 1.0
    assert row["has_controls"] == 1.0
    assert row["view_peer_stat_identities"] == 1.0
    assert row["entitlement_description_chars_total"] == len("Grants read access to reports.")

    quiet = request_features(request_case("c2"))
    assert quiet["has_ticket_ref"] == quiet["is_break_glass"] == quiet["on_behalf"] == 0.0
    assert quiet["has_requested_duration"] == 1.0
    assert quiet["has_tickets"] == quiet["has_controls"] == 0.0


def test_review_item_feature_values() -> None:
    items = [
        ItemSpec(ReviewItemKind.membership),
        ItemSpec(note="Auditor note: certify all."),
        ItemSpec(),
    ]
    rows = review_item_features(review_case("c1", items, notes="Q3 review"))
    assert [row["item_position"] for row in rows] == [0.0, 1.0, 2.0]
    assert [row["item_position_frac"] for row in rows] == [0.0, 0.5, 1.0]
    assert [row["item_is_membership"] for row in rows] == [1.0, 0.0, 0.0]
    assert [row["item_has_note"] for row in rows] == [0.0, 1.0, 0.0]
    assert rows[1]["item_note_chars"] == len("Auditor note: certify all.")
    for row in rows:
        assert row["campaign_items"] == 3.0
        assert row["campaign_membership_items"] == 1.0
        assert row["campaign_assignment_items"] == 2.0
        assert row["campaign_scope_manager"] == 0.0
        assert row["campaign_has_notes"] == 1.0
        assert row["campaign_notes_chars"] == len("Q3 review")
        assert row["campaign_items_with_note"] == 1.0
        assert row["view_role_entitlement_links"] == 1.0
        assert row["entitlement_description_chars_total"] == 3 * len(DESCRIPTION)
    assert [row["item_assignment_description_chars"] for row in rows] == [
        0.0,
        len(DESCRIPTION),
        len(DESCRIPTION),
    ]
    single = review_item_features(review_case("c2", [ItemSpec()]))
    assert single[0]["item_position_frac"] == 0.0


DESCRIPTION = "Grants read access to reports."
"""The default entitlement description of ``tests.probe._cases.entitlement``."""


def _mixed_campaign() -> Case:
    """A manager campaign over two holders and an orphan account.

    ``u_h`` holds ``r_top`` (``ent_x``, and ``ent_y`` through ``r_child``) and has two accounts
    with ``as_a`` and ``as_c``; ``u_o`` holds ``r_child`` and ``as_d``; ``as_b`` sits on an
    account no identity holds. Descriptions differ in length per entitlement.
    """
    descriptions = {"ent_x": "x" * 10, "ent_y": "y" * 40, "ent_a": "a", "ent_b": "b" * 7}
    descriptions |= {"ent_c": "c" * 3, "ent_d": "d" * 25}
    entities: list[Any] = [
        identity("u_h"),
        identity("u_o"),
        identity("u_mgr", manager_id=None),
        application(),
        application("app_b"),
        account("acct_h", "u_h"),
        account("acct_h2", "u_h", app_id="app_b"),
        account("acct_o", "u_o"),
        account("acct_orphan", None),
        role("r_top", ["ent_x"], child_roles=["r_child"]),
        role("r_child", ["ent_y"]),
        membership("rm_1", "u_h", "r_top"),
        membership("rm_2", "u_o", "r_child"),
        assignment("as_a", "acct_h", "ent_a"),
        assignment("as_b", "acct_orphan", "ent_b"),
        assignment("as_c", "acct_h2", "ent_c"),
        assignment("as_d", "acct_o", "ent_d"),
        *(entitlement(e, description=text) for e, text in descriptions.items()),
        usage("use_hx", "acct_h", "ent_x", day(-200)),
        usage("use_hy", "acct_h", "ent_y", day(-200)),
        usage("use_ha", "acct_h", "ent_a", day(-200)),
        usage("use_hc", "acct_h2", "ent_c", day(-200)),
        usage("use_ob", "acct_orphan", "ent_b", day(-200)),
        usage("use_oy", "acct_o", "ent_y", day(-200)),
        usage("use_od", "acct_o", "ent_d", day(-200)),
    ]
    refs = [
        ("rm_1", ReviewItemKind.membership),
        ("as_b", ReviewItemKind.assignment),
        ("as_d", ReviewItemKind.assignment),
        ("rm_2", ReviewItemKind.membership),
    ]
    items = tuple(
        ReviewItem(item_id=f"rv_{i}", kind=kind, ref=ref) for i, (ref, kind) in enumerate(refs)
    )
    campaign = Campaign(
        campaign_id="camp",
        reviewer_id="u_mgr",
        scope_kind=CampaignScopeKind.manager,
        scope_ref="u_mgr",
        items=items,
    )
    gold = ReviewGold(
        items=tuple(
            ReviewItemGold(
                item_id=item.item_id, decision=ReviewDecision.CERTIFY, risk_tier=RiskTier.low
            )
            for item in items
        )
    )
    return make_case("mixed", Task.access_review, make_view(*entities, campaign=campaign), gold)


def test_review_item_features_read_an_assignments_description_only() -> None:
    rows = review_item_features(_mixed_campaign())
    # Memberships rm_1 and rm_2 read nothing their roles reach; as_b and as_d read their own
    # entitlement's description.
    assert [row["item_assignment_description_chars"] for row in rows] == [0.0, 7.0, 25.0, 0.0]
    assert {row["campaign_scope_manager"] for row in rows} == {1.0}
    assert {row["entitlement_description_chars_total"] for row in rows} == {86.0}


def _grow_role(view: CaseView, role_id: str, extra: int, description: str) -> CaseView:
    """``view`` with ``extra`` more entitlements in ``role_id``'s expansion, through a child
    role each, all described by ``description`` and each with a usage record on ``acct_h``."""
    added = [f"ent_grow_{i}" for i in range(extra)]
    children = [role(f"r_grow_{i}", [e]) for i, e in enumerate(added)]
    grown = view.roles[role_id].model_copy(
        update={"child_roles": (*view.roles[role_id].child_roles, *(c.id for c in children))}
    )
    return add_entities(
        view,
        grown,
        *children,
        *(entitlement(e, description=description) for e in added),
        *(usage(f"use_grow_{i}", "acct_h", e, day(-200)) for i, e in enumerate(added)),
    )


ITEM_FEATURES = tuple(name for name in REVIEW_ITEM_FEATURES if name.startswith("item_"))
"""The features of one review item, as opposed to its campaign's or view's."""


@pytest.mark.parametrize("kind", ["business", "birthright"])
def test_membership_features_ignore_what_the_role_reaches(kind: str) -> None:
    """Role kind decides HYG-001 and HYG-005, and in generated worlds a role's expansion size,
    the usage records on it and its descriptions all track that kind (see ``probe.features``).
    So a membership item's own features must not move when its role reaches more entitlements,
    with longer descriptions and more usage, or when the role changes kind."""
    case = review_case("c1", [ItemSpec(ReviewItemKind.membership), ItemSpec()])
    view = case.view
    retyped = view.roles["r_0"].model_copy(update={"kind": RoleKind(kind)})
    grown = _grow_role(add_entities(view, retyped), "r_0", 4, "x" * 120)
    before = review_item_features(case)
    after = review_item_features(case.model_copy(update={"view": grown}))
    for row_before, row_after in zip(before, after, strict=True):
        assert {k: row_after[k] for k in ITEM_FEATURES} == {k: row_before[k] for k in ITEM_FEATURES}


def test_review_features_ignore_account_ownership() -> None:
    """Whether an account has an owner is what HYG-002 and HYG-003 read, so switching an
    assignment's account between owned, orphaned and service must not move a single feature of
    any item. Per-holder counts would leak it through their values though their names look
    harmless: an account with no identity has no other accounts and no memberships."""
    base = _mixed_campaign()
    views = [
        add_entities(base.view, account_held("acct_orphan", ownership, "u_o"))
        for ownership in ACCOUNT_OWNERSHIPS
    ]
    switched = [v.accounts["acct_orphan"] for v in views]
    assert [(a.identity_id, a.kind.value, a.owner_id) for a in switched] == [
        ("u_o", "human", None),
        (None, "human", None),
        (None, "service", "u_o"),
    ]
    rows = [review_item_features(_replace(base, accounts=v.accounts)) for v in views]
    assert rows[0] == rows[1] == rows[2]
    # Whole campaigns too: which accounts are orphaned (and so which items are REVOKE) leaves
    # every row as it was.
    plain = review_item_features(ownership_campaign("c", ()))
    for orphaned in ({0}, {2, 5, 9}, set(range(10))):
        assert review_item_features(ownership_campaign("c", orphaned)) == plain


def test_review_item_features_need_a_complete_view() -> None:
    case = review_case("c1", [ItemSpec(ReviewItemKind.membership)])
    broken = case.model_copy(update={"view": case.view.model_copy(update={"memberships": {}})})
    with pytest.raises(OracleInputError, match="unknown membership"):
        review_item_features(broken)


def test_review_item_features_need_the_entitlement_they_read() -> None:
    """An assignment's entitlement is read for its description; a view without it is
    incomplete, not a row of zeros."""
    case = review_case("c1", [ItemSpec(ReviewItemKind.assignment)])
    entitlements = {k: e for k, e in case.view.entitlements.items() if k != "ent_a_0"}
    with pytest.raises(OracleInputError, match="entitlement 'ent_a_0' is referenced"):
        review_item_features(_replace(case, entitlements=entitlements))


def test_request_features_need_the_requested_entitlement() -> None:
    case = request_case("c1")
    with pytest.raises(OracleInputError, match="entitlement 'ent_req' is referenced"):
        request_features(_replace(case, entitlements={}))


def test_provenance_key_lists_are_pinned() -> None:
    assert sorted(TEXT_FAMILY_KEYS) == [
        "attack_family",
        "description_family",
        "justification_family",
        "note_family",
        "ticket_family",
    ]
    assert sorted(NON_FEATURE_KEYS) == ["attack_field", "id_rename", "scenario", "twin_edit"]
    assert TEXT_FAMILY_KEYS.isdisjoint(NON_FEATURE_KEYS)


def test_only_text_family_provenance_keys_become_features() -> None:
    provenance = {
        "scenario": "sod_unmitigated",
        "twin_edit": "employment_type",
        "attack_field": "req_x.justification",
        "justification_family": "just.dev.a",
        "attack_family": "atk.injection.formal.dev",
    }
    case = request_case("c1", provenance=provenance)
    families = family_features(case)
    assert families == {
        "family:attack_family=atk.injection.formal.dev": 1.0,
        "family:justification_family=just.dev.a": 1.0,
    }
    row = request_features(case)
    assert {k for k in row if is_family_feature(k)} == set(families)
    assert all(k.startswith(FAMILY_PREFIX) for k in families)
    items = review_item_features(review_case("c2", [ItemSpec()], provenance=provenance))
    assert {k for k in items[0] if is_family_feature(k)} == set(families)
    every_family = {key: "fam" for key in TEXT_FAMILY_KEYS}
    assert len(family_features(request_case("c3", provenance=every_family))) == 5


def test_an_id_rename_map_is_never_a_feature() -> None:
    """An ID-renaming twin records its old -> new map as canonical JSON under ``id_rename``
    (``tasks.twins.ID_RENAME_KEY``). It must pass the provenance check, so the
    ``surface_probe`` baseline can answer the twin, and must add no feature: the map's IDs are
    random, and a one-hot of it would be unique to the case."""
    from identitybench.tasks.twins import ID_RENAME_KEY

    assert ID_RENAME_KEY in NON_FEATURE_KEYS
    base_provenance = {"justification_family": "just.dev.a", "twin_edit": "renamed 2 IDs"}
    mapping = json.dumps(
        {"id_alpha": "id_48213", "req_x": "req_90177"}, sort_keys=True, separators=(",", ":")
    )
    renamed = {**base_provenance, ID_RENAME_KEY: mapping}
    request = request_case("c1", provenance=renamed)
    assert family_features(request) == {"family:justification_family=just.dev.a": 1.0}
    assert request_features(request) == request_features(
        request_case("c1", provenance=base_provenance)
    )
    review = review_case("c2", [ItemSpec(), ItemSpec()], provenance=renamed)
    assert review_item_features(review) == review_item_features(
        review_case("c2", [ItemSpec(), ItemSpec()], provenance=base_provenance)
    )


@pytest.mark.parametrize(
    "key",
    # A label-deciding key that a suffix rule would have taken for a family, and family keys
    # that it would have dropped.
    ["scenario_family", "justification_template", "text_families", "template"],
)
def test_an_unclassified_provenance_key_is_an_error(key: str) -> None:
    case = request_case("c1", provenance={"justification_family": "just.dev.a", key: "x"})
    with pytest.raises(ValueError, match=rf"provenance keys \['{key}'\] are neither"):
        family_features(case)
    with pytest.raises(ValueError, match=r"classify them in probe\.features"):
        request_features(case)
    review = review_case("c2", [ItemSpec()], provenance={key: "x"})
    with pytest.raises(ValueError, match="are neither text template families"):
        review_item_features(review)


def _with_rule_inputs(view: CaseView, holder: str) -> CaseView:
    """``view`` plus a ticket, a control for ``holder`` and the view's usage records, all set
    to plausible "quiet" values that ``_flip_rule_inputs`` then changes."""
    return add_entities(view, ticket("INC-1"), control("mc_1", holder))


def _flip_rule_inputs(view: CaseView) -> CaseView:
    """Change every rule input the probe must not read, keeping counts and text lengths.

    Identities (employment type, status, manager chain, hire and move dates, department,
    location), applications and entitlements (risk, privilege, classification, eligibility),
    accounts (status, kind), roles (kind), records (dates, sources), usage telemetry, tickets
    (status, type, dates, assignee, related apps), controls (dates, rule), the request's dates
    and duration, peer share values and the policy pack's SoD sides.
    """
    identities = {
        k: i.model_copy(
            update={
                "employment_type": EmploymentType.contractor,
                "status": IdentityStatus.terminated,
                "manager_id": "u_someone_else",
                "hire_date": day(-2),
                "move_date": day(-3),
                "department": "Legal",
                "location": Location(country="DE", region="EU"),
            }
        )
        for k, i in view.identities.items()
    }
    applications = {
        k: a.model_copy(
            update={
                "risk_tier": RiskTier.critical,
                "data_classification": DataClassification.restricted,
                "sox_in_scope": True,
                "environment": Environment.nonprod,
                "owner_id": "u_someone_else",
            }
        )
        for k, a in view.applications.items()
    }
    entitlements = {
        k: e.model_copy(
            update={
                "privilege": Privilege.admin,
                "privileged": True,
                "risk_tier": RiskTier.critical,
                "data_classification": DataClassification.restricted,
                "requestable": False,
                "eligible_employment_types": (EmploymentType.vendor,),
            }
        )
        for k, e in view.entitlements.items()
    }
    accounts = {
        k: a.model_copy(update={"status": AccountStatus.disabled, "kind": AccountKind.service})
        for k, a in view.accounts.items()
    }
    roles = {k: r.model_copy(update={"kind": RoleKind.birthright}) for k, r in view.roles.items()}
    memberships = {
        k: m.model_copy(
            update={"source": "birthright", "granted_on": day(-2), "expires_on": day(1)}
        )
        for k, m in view.memberships.items()
    }
    assignments = {
        k: a.model_copy(update={"source": "direct", "granted_on": day(-9), "expires_on": day(-5)})
        for k, a in view.assignments.items()
    }
    usage_records = {
        k: u.model_copy(update={"telemetry": TelemetryStatus.unavailable, "last_used_on": None})
        for k, u in view.usage.items()
    }
    tickets = {
        k: t.model_copy(
            update={
                "status": TicketStatus.closed,
                "type": TicketType.project,
                "opened_on": day(-400),
                "assignee_id": None,
                "related_app_ids": ("app_elsewhere",),
            }
        )
        for k, t in view.tickets.items()
    }
    controls = {
        k: c.model_copy(update={"granted_on": day(-300), "expires_on": day(-1), "rule_id": "X"})
        for k, c in view.mitigating_controls.items()
    }
    peer_stats = {
        holder: {e: 1.0 - share for e, share in shares.items()}
        for holder, shares in view.peer_stats.items()
    }
    request = view.request
    if request is not None:
        request = request.model_copy(
            update={"submitted_on": day(-30), "requested_duration_days": 365}
        )
    pack = PolicyPack(
        org_name=view.org_name,
        sod=(SodRuleSides(rule_id="SOD-001", title="x", side_a=tuple(entitlements), side_b=()),),
    )
    return view.model_copy(
        update={
            "identities": identities,
            "applications": applications,
            "entitlements": entitlements,
            "accounts": accounts,
            "roles": roles,
            "memberships": memberships,
            "assignments": assignments,
            "usage": usage_records,
            "tickets": tickets,
            "mitigating_controls": controls,
            "peer_stats": peer_stats,
            "request": request,
            "policy_pack": pack,
            "as_of": day(300),
        }
    )


def _flip_metadata(case: Case) -> dict[str, Any]:
    """Generator metadata the probe must not read: labels, tier, tags, variant, split and the
    label-deciding provenance entries."""
    return {
        "tier": Tier.L3,
        "tags": ("sod",),
        "variant": "counterfactual",
        "provenance": {**case.provenance, "scenario": "sod", "twin_edit": "privileged"},
        "split": Split.test,
    }


def test_request_features_ignore_rule_inputs_and_metadata() -> None:
    """Changing what the rules read or what only the generator knows must not move a single
    feature."""
    case = request_case(
        "c1",
        provenance={"justification_family": "just.dev.a"},
        ticket_ref="INC-1",
        peer_share=0.4,
        extra=[usage("use_req", "acct_ben", "ent_req", day(-10))],
    )
    case = case.model_copy(update={"view": _with_rule_inputs(case.view, "u_ben")})
    changed = case.model_copy(
        update={
            "view": _flip_rule_inputs(case.view),
            "gold": request_case("c9", Label.N).gold,
            **_flip_metadata(case),
        }
    )
    assert request_features(changed) == request_features(case)


@pytest.mark.parametrize("campaign", ["single_holder", "mixed"])
def test_review_features_ignore_rule_inputs_and_metadata(campaign: str) -> None:
    if campaign == "mixed":
        case = _mixed_campaign()
    else:
        case = review_case("c1", [ItemSpec(), ItemSpec(ReviewItemKind.membership)])
    case = case.model_copy(update={"view": _with_rule_inputs(case.view, "u_h")})
    assert case.view.peer_stats == {}
    case = case.model_copy(
        update={"view": case.view.model_copy(update={"peer_stats": {"u_h": {"ent_y": 0.2}}})}
    )
    gold = case.gold.model_copy(  # type: ignore[union-attr]
        update={
            "items": tuple(
                item.model_copy(update={"decision": ReviewDecision.REVOKE})
                for item in case.gold.items  # type: ignore[union-attr]
            )
        }
    )
    changed = case.model_copy(
        update={"view": _flip_rule_inputs(case.view), "gold": gold, **_flip_metadata(case)}
    )
    assert review_item_features(changed) == review_item_features(case)


def test_view_features_count_collections() -> None:
    case = jml_case("j1")
    row = view_features(case.view)
    assert row["view_roles"] == 6.0
    assert row["view_role_entitlement_links"] == 6.0
    assert row["view_role_child_links"] == 0.0
    assert row["view_entitlements"] == 5.0


def test_features_reject_the_wrong_task() -> None:
    with pytest.raises(ValueError, match="no access request"):
        request_features(jml_case("j1"))
    with pytest.raises(ValueError, match="no review campaign"):
        review_item_features(request_case("c1"))


def test_world_built_variants_have_features() -> None:
    """Twins and adversarial copies carry provenance keys the bases don't (``attack_field``,
    ``twin_edit``, ``id_rename``); each must be classified, or the ``surface_probe`` baseline
    couldn't answer them. Only text family keys become features."""
    pytest.importorskip("identitybench.tasks.adversarial")
    pytest.importorskip("identitybench.tasks.twins")
    pytest.importorskip("identitybench.tasks.requests")
    pytest.importorskip("identitybench.tasks.reviews")
    built = world_cases_with_variants()
    variants = built.adversarial + built.twins
    assert {key for case in built.adversarial for key in case.provenance} >= {
        "attack_family",
        "attack_field",
    }
    assert {key for case in built.twins for key in case.provenance} >= {"twin_edit", "id_rename"}
    for case in variants:
        rows = (
            [request_features(case)]
            if case.task is Task.access_request
            else review_item_features(case)
        )
        families = {
            name.removeprefix(FAMILY_PREFIX).split("=", 1)[0]
            for row in rows
            for name in row
            if is_family_feature(name)
        }
        assert families <= TEXT_FAMILY_KEYS, case.case_id
