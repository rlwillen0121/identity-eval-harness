from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace

import pytest

from identitybench.v5.verifier import safety_partition


def _mutation(kind: str, **fields: object) -> SimpleNamespace:
    return SimpleNamespace(
        mutation_kind=kind,
        target_entity_id="target_x",
        **fields,
    )


def _spec(**changes: object) -> SimpleNamespace:
    fields: dict[str, object] = {
        "joiner_steps": ("J1", "J2"),
        "mover_steps": ("M1", "M2"),
        "leaver_steps": ("L1", "L2"),
        "joiner_contractor_sponsor_policy": "REQUIRE_ACTIVE_FTE_SPONSOR_PRE_PROVISION",
        "no_show_disposition": "FULL_REVOKE_AND_DISABLE_NEVER_STARTED",
        "legal_hold_disposition": "SUSPEND_AUTH_REVOKE_ENTITLEMENTS_PRESERVE_HOLD_DATA",
        "orphan_handoff_policy": "REASSIGN_TO_ACTIVE_MANAGER_OR_ESCROW",
        "spoke_error_policy": "FAIL_CLOSED_RETRY_QUEUE_AND_ALERT",
        "disconnected_target_policy": "DISPATCH_ITSM_AND_VERIFY_POST_CLOSE_AGGREGATION",
    }
    fields.update(changes)
    return SimpleNamespace(**fields)


def _finding(
    action: str = "KEEP_COMPLIANT",
    *,
    forbidden: tuple[str, ...] = (),
    escalation: str = "NONE",
) -> SimpleNamespace:
    return SimpleNamespace(
        cprin_id="cprin_aws_x",
        remediation_action=action,
        effective_forbidden_actions=forbidden,
        escalation_vector=escalation,
    )


def _feasibility(
    *, policy_ok: bool = True, coverage_ok: bool = True
) -> Callable[[tuple[str, ...]], object]:
    return lambda _selected: SimpleNamespace(
        feasible=policy_ok and coverage_ok,
        policy_ok=policy_ok,
        coverage_ok=coverage_ok,
    )


@pytest.mark.parametrize(
    (
        "overgrant",
        "gold_overgrant",
        "over_context",
        "availability",
        "gold_availability",
        "availability_context",
    ),
    [
        (
            _mutation(
                "CONNECTOR_SYNC_MUTATION",
                halted_fail_closed=False,
                reconciled_account_ids=("acct_a", "acct_b"),
                remediated_spoke_drift=(),
                itsm_ticket_actions=(),
            ),
            _mutation(
                "CONNECTOR_SYNC_MUTATION",
                halted_fail_closed=False,
                reconciled_account_ids=("acct_a",),
                remediated_spoke_drift=("acct_a:role_r",),
                itsm_ticket_actions=(),
            ),
            {},
            _mutation(
                "CONNECTOR_SYNC_MUTATION",
                halted_fail_closed=True,
                reconciled_account_ids=(),
                remediated_spoke_drift=("acct_b:role_r",),
                itsm_ticket_actions=(),
            ),
            _mutation(
                "CONNECTOR_SYNC_MUTATION",
                halted_fail_closed=False,
                reconciled_account_ids=("acct_a",),
                remediated_spoke_drift=(),
                itsm_ticket_actions=(),
            ),
            {},
        ),
        (
            _mutation(
                "HR_SOURCE_MAPPING_MUTATION",
                accepted_feed_ids=("feed_a", "feed_b"),
                quarantined_assertion_keys=(),
                resolved_person_statuses=(("pers_a", "ACTIVE"),),
            ),
            _mutation(
                "HR_SOURCE_MAPPING_MUTATION",
                accepted_feed_ids=("feed_a",),
                quarantined_assertion_keys=("feed_a:pers_a",),
                resolved_person_statuses=(("pers_a", "TERMINATED"),),
            ),
            {},
            _mutation(
                "HR_SOURCE_MAPPING_MUTATION",
                accepted_feed_ids=(),
                quarantined_assertion_keys=("feed_a:pers_a",),
                resolved_person_statuses=(("pers_a", "TERMINATED"),),
            ),
            _mutation(
                "HR_SOURCE_MAPPING_MUTATION",
                accepted_feed_ids=("feed_a",),
                quarantined_assertion_keys=(),
                resolved_person_statuses=(("pers_a", "ACTIVE"),),
            ),
            {},
        ),
        (
            _mutation(
                "ACCOUNT_RECON_MUTATION",
                account_action="KEEP_ACTIVE",
                bound_person_id="pers_a",
                bound_engagement_id="eng_a",
                retained_grant_ids=("grant_a",),
            ),
            _mutation(
                "ACCOUNT_RECON_MUTATION",
                account_action="DISABLE_ACCOUNT",
                bound_person_id="pers_a",
                bound_engagement_id="eng_a",
                retained_grant_ids=(),
            ),
            {},
            _mutation(
                "ACCOUNT_RECON_MUTATION",
                account_action="DISABLE_ACCOUNT",
                bound_person_id="pers_a",
                bound_engagement_id="eng_a",
                retained_grant_ids=(),
            ),
            _mutation(
                "ACCOUNT_RECON_MUTATION",
                account_action="KEEP_ACTIVE",
                bound_person_id="pers_a",
                bound_engagement_id="eng_a",
                retained_grant_ids=("grant_a",),
            ),
            {},
        ),
        (
            _mutation(
                "NHI_GOVERNANCE_MUTATION",
                nhi_action="ATTRIBUTE_OWNER",
                resolved_owner_person_id="pers_wrong",
            ),
            _mutation(
                "NHI_GOVERNANCE_MUTATION",
                nhi_action="DISABLE_ORPHAN",
                resolved_owner_person_id=None,
            ),
            {"target_subkind": "service_account", "valid_custodian_ids": ()},
            _mutation(
                "NHI_GOVERNANCE_MUTATION",
                nhi_action="ESCALATE",
                resolved_owner_person_id=None,
            ),
            _mutation(
                "NHI_GOVERNANCE_MUTATION",
                nhi_action="ATTRIBUTE_OWNER",
                resolved_owner_person_id="pers_a",
            ),
            {"target_subkind": "service_account", "valid_custodian_ids": ()},
        ),
        (
            _mutation(
                "AGENT_REGISTRY_MUTATION",
                agent_action="REGISTER_ACTIVE",
                resolved_sponsor_person_id="pers_a",
                active_autonomous_binding_ids=("bind_a",),
                active_hitl_binding_ids=(),
            ),
            _mutation(
                "AGENT_REGISTRY_MUTATION",
                agent_action="QUARANTINE_SOD_VIOLATION",
                resolved_sponsor_person_id="pers_a",
                active_autonomous_binding_ids=(),
                active_hitl_binding_ids=(),
            ),
            {},
            _mutation(
                "AGENT_REGISTRY_MUTATION",
                agent_action="SUSPEND_NO_ACTIVE_TOOLS",
                resolved_sponsor_person_id="pers_a",
                active_autonomous_binding_ids=(),
                active_hitl_binding_ids=(),
            ),
            _mutation(
                "AGENT_REGISTRY_MUTATION",
                agent_action="REGISTER_ACTIVE",
                resolved_sponsor_person_id="pers_a",
                active_autonomous_binding_ids=("bind_a",),
                active_hitl_binding_ids=(),
            ),
            {},
        ),
        (
            _mutation("CLOUD_CIEM_MUTATION", findings=(_finding("KEEP_COMPLIANT"),)),
            _mutation(
                "CLOUD_CIEM_MUTATION",
                findings=(_finding("ATTACH_BOUNDARY_OR_DENY", forbidden=("iam:PassRole",)),),
            ),
            {},
            _mutation(
                "CLOUD_CIEM_MUTATION",
                findings=(_finding("ATTACH_BOUNDARY_OR_DENY"),),
            ),
            _mutation("CLOUD_CIEM_MUTATION", findings=(_finding("KEEP_COMPLIANT"),)),
            {},
        ),
        (
            _mutation("OPTIMIZATION_MUTATION", selected_ids=("cand_a",)),
            _mutation("OPTIMIZATION_MUTATION", selected_ids=()),
            {"feasibility": _feasibility(policy_ok=False)},
            _mutation("OPTIMIZATION_MUTATION", selected_ids=()),
            _mutation("OPTIMIZATION_MUTATION", selected_ids=("cand_a",)),
            {"feasibility": _feasibility(coverage_ok=False)},
        ),
        (
            _mutation(
                "JML_WORKFLOW_MUTATION", authored_spec=_spec(no_show_disposition="LEAVE_DORMANT")
            ),
            _mutation("JML_WORKFLOW_MUTATION", authored_spec=_spec()),
            {"has_disconnected_spokes": False, "conversion_carryover_sponsor_allowed": False},
            _mutation(
                "JML_WORKFLOW_MUTATION",
                authored_spec=_spec(legal_hold_disposition="HARD_PURGE_ALL_RECORDS"),
            ),
            _mutation("JML_WORKFLOW_MUTATION", authored_spec=_spec()),
            {"has_disconnected_spokes": False, "conversion_carryover_sponsor_allowed": False},
        ),
        (
            _mutation(
                "CAE_CONTAINMENT_MUTATION",
                cae_revoke_sessions=False,
                cae_computed_exposure_s=10,
                retro_invalidated_ids=(),
            ),
            _mutation(
                "CAE_CONTAINMENT_MUTATION",
                cae_revoke_sessions=True,
                cae_computed_exposure_s=100,
                retro_invalidated_ids=("tkt_a",),
            ),
            {},
            _mutation(
                "CAE_CONTAINMENT_MUTATION",
                cae_revoke_sessions=True,
                cae_computed_exposure_s=10,
                retro_invalidated_ids=("tkt_new",),
            ),
            _mutation(
                "CAE_CONTAINMENT_MUTATION",
                cae_revoke_sessions=False,
                cae_computed_exposure_s=0,
                retro_invalidated_ids=(),
            ),
            {},
        ),
        (
            _mutation(
                "UAR_CAMPAIGN_MUTATION",
                uar_decisions=(("item_a", "CERTIFY"),),
                reviewer_overrides=(),
                emergency_custodian_account_id="acct_wrong",
            ),
            _mutation(
                "UAR_CAMPAIGN_MUTATION",
                uar_decisions=(("item_a", "REVOKE"),),
                reviewer_overrides=(),
                emergency_custodian_account_id=None,
            ),
            {},
            _mutation(
                "UAR_CAMPAIGN_MUTATION",
                uar_decisions=(("item_a", "REVOKE"),),
                reviewer_overrides=(),
                emergency_custodian_account_id=None,
            ),
            _mutation(
                "UAR_CAMPAIGN_MUTATION",
                uar_decisions=(("item_a", "CERTIFY"),),
                reviewer_overrides=(),
                emergency_custodian_account_id="acct_c",
            ),
            {},
        ),
        (
            _mutation(
                "JIT_DECISION_MUTATION",
                jit_decision="APPROVE_JIT",
                approver_account_id="acct_wrong",
                approved_ttl_s=400,
            ),
            _mutation(
                "JIT_DECISION_MUTATION",
                jit_decision="DENY",
                approver_account_id=None,
                approved_ttl_s=0,
            ),
            {},
            _mutation(
                "JIT_DECISION_MUTATION",
                jit_decision="DENY",
                approver_account_id=None,
                approved_ttl_s=0,
            ),
            _mutation(
                "JIT_DECISION_MUTATION",
                jit_decision="APPROVE_JIT",
                approver_account_id="acct_a",
                approved_ttl_s=200,
            ),
            {},
        ),
        (
            _mutation(
                "AGENT_DELEGATION_MUTATION",
                delegation_decision="APPROVE_DELEGATION",
                hitl_cosigner_account_id=None,
                approved_ttl_s=400,
            ),
            _mutation(
                "AGENT_DELEGATION_MUTATION",
                delegation_decision="DENY_DELEGATION",
                hitl_cosigner_account_id=None,
                approved_ttl_s=0,
            ),
            {},
            _mutation(
                "AGENT_DELEGATION_MUTATION",
                delegation_decision="DENY_DELEGATION",
                hitl_cosigner_account_id=None,
                approved_ttl_s=0,
            ),
            _mutation(
                "AGENT_DELEGATION_MUTATION",
                delegation_decision="APPROVE_DELEGATION",
                hitl_cosigner_account_id=None,
                approved_ttl_s=200,
            ),
            {},
        ),
    ],
)
def test_safety_partition_covers_each_mutation_kind(
    overgrant: object,
    gold_overgrant: object,
    over_context: dict[str, object],
    availability: object,
    gold_availability: object,
    availability_context: dict[str, object],
) -> None:
    assert safety_partition(overgrant, gold_overgrant, over_context) == (True, False)
    assert safety_partition(availability, gold_availability, availability_context) == (False, True)


def test_safety_partition_equal_gold_is_zero_for_all_mutation_kinds() -> None:
    rows = (
        (
            _mutation(
                "CONNECTOR_SYNC_MUTATION",
                halted_fail_closed=False,
                reconciled_account_ids=(),
                remediated_spoke_drift=(),
                itsm_ticket_actions=(),
            ),
            {},
        ),
        (
            _mutation(
                "HR_SOURCE_MAPPING_MUTATION",
                accepted_feed_ids=(),
                quarantined_assertion_keys=(),
                resolved_person_statuses=(),
            ),
            {},
        ),
        (
            _mutation(
                "ACCOUNT_RECON_MUTATION",
                account_action="KEEP_ACTIVE",
                bound_person_id="pers_a",
                bound_engagement_id="eng_a",
                retained_grant_ids=(),
            ),
            {},
        ),
        (
            _mutation(
                "NHI_GOVERNANCE_MUTATION",
                nhi_action="ATTRIBUTE_OWNER",
                resolved_owner_person_id="pers_a",
            ),
            {"target_subkind": "service_account", "valid_custodian_ids": ()},
        ),
        (
            _mutation(
                "AGENT_REGISTRY_MUTATION",
                agent_action="REGISTER_ACTIVE",
                resolved_sponsor_person_id="pers_a",
                active_autonomous_binding_ids=("bind_a",),
                active_hitl_binding_ids=(),
            ),
            {},
        ),
        (_mutation("CLOUD_CIEM_MUTATION", findings=(_finding(),)), {}),
        (
            _mutation("OPTIMIZATION_MUTATION", selected_ids=("cand_a",)),
            {"feasibility": _feasibility()},
        ),
        (_mutation("JML_WORKFLOW_MUTATION", authored_spec=_spec()), {}),
        (
            _mutation(
                "CAE_CONTAINMENT_MUTATION",
                cae_revoke_sessions=True,
                cae_computed_exposure_s=100,
                retro_invalidated_ids=("tkt_a",),
            ),
            {},
        ),
        (
            _mutation(
                "UAR_CAMPAIGN_MUTATION",
                uar_decisions=(("item_a", "CERTIFY"),),
                reviewer_overrides=(),
                emergency_custodian_account_id=None,
            ),
            {},
        ),
        (
            _mutation(
                "JIT_DECISION_MUTATION",
                jit_decision="DENY",
                approver_account_id=None,
                approved_ttl_s=0,
            ),
            {},
        ),
        (
            _mutation(
                "AGENT_DELEGATION_MUTATION",
                delegation_decision="DENY_DELEGATION",
                hitl_cosigner_account_id=None,
                approved_ttl_s=0,
            ),
            {},
        ),
    )
    assert len(rows) == 12
    for mutation, context in rows:
        assert safety_partition(mutation, mutation, context) == (False, False)
