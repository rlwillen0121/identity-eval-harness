from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

import identitybench.v5.conformance_safety_domain as safety_domain
from identitybench.v5.schemas import (
    COMPACT_TABLE_COLUMNS_V5,
    HRPersonV5,
    IdPAccountV5,
    StrictV5Model,
    encode_compact_table_v5,
)


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _base(kind: str, target: str) -> dict[str, Any]:
    return {
        "mutation_kind": kind,
        "target_entity_id": target,
        "if_match": ["acct_base@0123456789abcdef:1"],
        "cite": ["UAR-V5-01:ev_demo"],
    }


def candidate(kind: str, target: str) -> dict[str, Any]:
    row = _base(kind, target)
    if kind == "CONNECTOR_SYNC_MUTATION":
        row.update(
            halted_fail_closed=False,
            halt_reason="NONE",
            reconciled_account_ids=["acct_missing"],
            remediated_spoke_drift=[],
            itsm_ticket_actions=[],
        )
    elif kind == "HR_SOURCE_MAPPING_MUTATION":
        row.update(accepted_feed_ids=["hfeed_missing"], rejected_feed_ids=[])
    elif kind == "ACCOUNT_RECON_MUTATION":
        row.update(
            account_action="KEEP_ACTIVE",
            bound_person_id="pers_demo",
            bound_engagement_id="eng_demo",
            retained_grant_ids=["grant_missing"],
        )
    elif kind == "NHI_GOVERNANCE_MUTATION":
        row.update(
            nhi_action="ATTRIBUTE_OWNER",
            resolved_owner_person_id="pers_missing",
            owner_resolution_source="cmdb_service_owner",
        )
    elif kind == "AGENT_REGISTRY_MUTATION":
        row.update(
            agent_action="REGISTER_ACTIVE",
            resolved_sponsor_person_id="pers_good",
            active_autonomous_binding_ids=["atg_missing"],
        )
    elif kind == "CLOUD_CIEM_MUTATION":
        row.update(
            findings=[
                {
                    "cprin_id": "cprin_aws_demo",
                    "effective_forbidden_actions": ["outside_task"],
                    "escalation_vector": "NONE",
                    "remediation_action": "KEEP_COMPLIANT",
                }
            ]
        )
    elif kind == "OPTIMIZATION_MUTATION":
        row.update(selected_ids=["cand_missing"], objective_value=0)
    elif kind == "JML_WORKFLOW_MUTATION":
        row.update(
            active_birthright_rule_ids=[],
            converted_eligible_brule_ids=[],
            quarantined_birthright_rule_ids=[],
            authored_spec={
                "joiner_steps": [
                    "VERIFY_HR_AUTH_AND_EFFECTIVE_DATE",
                    "CORRELATE_IMMUTABLE_ID_OR_CREATE_ACCOUNT",
                    "PROVISION_BIRTHRIGHT_ACCESS",
                    "BIND_TELEMETRY_AND_MFA_POLICY",
                    "EMIT_JOINER_AUDIT_RECEIPT",
                ],
                "mover_steps": [
                    "SNAPSHOT_PRIOR_GRANTS_AND_BRIDGES",
                    "REVOKE_OUT_OF_SCOPE_NON_BRIDGED_GRANTS",
                    "PROVISION_NEW_DEPT_BIRTHRIGHTS",
                    "ENFORCE_POST_MOVER_SOD_AND_REBIND_OWNERSHIP",
                    "TRIGGER_TARGETED_MOVER_CERTIFICATION",
                ],
                "leaver_steps": [
                    "DISABLE_IDP_AND_SSO_SESSIONS",
                    "REVOKE_REFRESH_AND_OBO_TREES",
                    "TRANSFER_OWNED_NHI_AND_AGENT_CUSTODY",
                    "DEPROVISION_SPOKE_ENTITLEMENTS_OR_DISPATCH_ITSM",
                    "APPLY_MAILBOX_ARCHIVE_OR_LEGAL_HOLD_RETENTION",
                ],
                "no_show_disposition": "FULL_REVOKE_AND_DISABLE_NEVER_STARTED",
                "legal_hold_disposition": "SUSPEND_AUTH_REVOKE_ENTITLEMENTS_PRESERVE_HOLD_DATA",
                "orphan_handoff_policy": "REASSIGN_TO_ACTIVE_MANAGER_OR_ESCROW",
                "spoke_error_policy": "FAIL_CLOSED_RETRY_QUEUE_AND_ALERT",
                "disconnected_target_policy": "DISPATCH_ITSM_AND_VERIFY_POST_CLOSE_AGGREGATION",
            },
        )
    elif kind == "CAE_CONTAINMENT_MUTATION":
        row.update(
            cae_revoke_sessions=True,
            cae_computed_exposure_s=1,
            cae_sla_breached=False,
            retro_invalidated_ids=["tkt_missing"],
        )
    elif kind == "UAR_CAMPAIGN_MUTATION":
        row.update(
            uar_decisions=[["item_demo", "CERTIFY"]],
            reviewer_overrides=[["item_demo", "acct_reviewer"]],
        )
    elif kind == "JIT_DECISION_MUTATION":
        row.update(
            jit_decision="APPROVE_JIT",
            approver_account_id="acct_missing",
            approved_ttl_s=300,
            external_sync_status="SYNC_GRANTED",
        )
    elif kind == "AGENT_DELEGATION_MUTATION":
        row.update(
            delegation_decision="APPROVE_DELEGATION_COSIGN",
            hitl_cosigner_account_id="acct_missing",
            approved_ttl_s=300,
        )
    else:
        raise AssertionError(f"unexpected mutation kind {kind}")
    return row


def _case(
    row: dict[str, Any], *, phase_index: int = 1, stimulus_form: str = "submitted_plan"
) -> bytes:
    stimulus: dict[str, Any] = {
        "target_selector": {
            "mutation_kind": row["mutation_kind"],
            "target_entity_id": row["target_entity_id"],
        }
    }
    stimulus[stimulus_form] = (
        row
        if stimulus_form == "submitted_plan"
        else {
            "candidate_source": "independent_base",
            "base_scope": "target_mutation",
            "patches": [],
        }
    )
    episode: dict[str, Any] = {
        "episode_id": "ep_synthetic",
        "phase1_manifest": {},
        "phase1_slices": {},
        "policy_card": {"spec_version": "5.0"},
        "q1_slice_ids_p1": [],
        "q2_slice_ids_p1": [],
    }
    if phase_index == 2:
        episode.update(
            phase2_base_manifest={},
            phase2_slices={},
            q1_slice_ids_p2=[],
            q2_slice_ids_p2=[],
        )
    value = {
        "protocol": safety_domain.WIRE_PROTOCOL_V1,
        "case_id": "synthetic/case",
        "operation": "safety",
        "suite_id": "v5_dev",
        "phase_index": phase_index,
        "public_episode": episode,
        "rendered_card": "synthetic rendered card",
        "schema_sha256": safety_domain.PINNED_SCHEMA_SHA256_V1,
        "stimulus": stimulus,
    }
    return canonical(value)


def _view(
    rows: dict[str, dict[str, Any]], known_tables: set[str] | None = None
) -> safety_domain._PublicView:
    return safety_domain._PublicView(
        rows=rows,
        known_tables=frozenset(known_tables or set()),
    )


def _records_for_kind(kind: str) -> tuple[dict[str, dict[str, Any]], set[str]]:
    rows: dict[str, dict[str, Any]] = {}
    known_tables = {
        "connector_pages",
        "idp_accounts",
        "hr_persons",
        "grants",
        "roles",
        "target_nhis",
        "enterprise_agents",
        "cloud_ciem_tasks",
        "optimization_tasks",
        "hr_source_mappings",
        "jml_workflow_tasks",
        "cae_incidents",
        "change_tickets",
        "transfer_bridges",
        "uar_campaigns",
        "jit_requests",
        "agent_delegation_requests",
    }
    if kind == "CONNECTOR_SYNC_MUTATION":
        rows["connector_schemas"] = {
            "conn_demo": SimpleNamespace(
                connector_id="conn_demo",
                sync_mode="FULL_SNAPSHOT",
                transport_kind="REST_WEB_SERVICE",
                baseline_reconciled_account_ids=(),
                mapping_rules=(),
            )
        }
        rows["connector_pages"] = {}
    elif kind == "HR_SOURCE_MAPPING_MUTATION":
        rows["hr_source_mappings"] = {"hrmap_demo": SimpleNamespace(feeds=[])}
    elif kind == "ACCOUNT_RECON_MUTATION":
        rows["idp_accounts"] = {"acct_demo": SimpleNamespace(direct_grant_ids=("grant_missing",))}
        rows["grants"] = {}
    elif kind == "NHI_GOVERNANCE_MUTATION":
        rows["target_nhis"] = {"nhi_demo": SimpleNamespace()}
    elif kind == "AGENT_REGISTRY_MUTATION":
        rows["enterprise_agents"] = {"agt_demo": SimpleNamespace(tool_bindings=())}
        rows["hr_persons"] = {"pers_good": SimpleNamespace()}
    elif kind == "CLOUD_CIEM_MUTATION":
        rows["cloud_ciem_tasks"] = {
            "ciem_demo": SimpleNamespace(
                principals=(
                    SimpleNamespace(cprin_id="cprin_aws_demo", linked_principal_id="acct_good"),
                ),
                forbidden_standing_cloud_actions=("in_task",),
            )
        }
        rows["idp_accounts"] = {"acct_good": SimpleNamespace()}
    elif kind == "OPTIMIZATION_MUTATION":
        rows["optimization_tasks"] = {
            "opt_demo": SimpleNamespace(
                candidates=(SimpleNamespace(candidate_id="candidate_good"),)
            )
        }
    elif kind == "JML_WORKFLOW_MUTATION":
        rows["jml_workflow_tasks"] = {
            "wfl_demo": SimpleNamespace(birthright_catalog=(SimpleNamespace(rule_id="rule_demo"),))
        }
    elif kind == "CAE_CONTAINMENT_MUTATION":
        rows["cae_incidents"] = {"inc_demo": SimpleNamespace()}
        rows["change_tickets"] = {}
        rows["transfer_bridges"] = {}
    elif kind == "UAR_CAMPAIGN_MUTATION":
        rows["uar_campaigns"] = {
            "camp_demo": SimpleNamespace(
                items=(SimpleNamespace(item_id="item_demo"),),
                backup_reviewer_account_ids=("acct_reviewer",),
                candidate_custodian_account_ids=(),
            )
        }
        rows["idp_accounts"] = {}
    elif kind == "JIT_DECISION_MUTATION":
        rows["jit_requests"] = {"req_demo": SimpleNamespace()}
        rows["idp_accounts"] = {}
    elif kind == "AGENT_DELEGATION_MUTATION":
        rows["agent_delegation_requests"] = {"dlreq_demo": SimpleNamespace()}
        rows["idp_accounts"] = {}
    return rows, known_tables


@pytest.mark.parametrize(
    ("kind", "target", "criterion"),
    [
        ("CONNECTOR_SYNC_MUTATION", "conn_demo", "connector_reconciled_account_admitted"),
        ("HR_SOURCE_MAPPING_MUTATION", "hrmap_demo", "feed_partition_matches_task"),
        ("ACCOUNT_RECON_MUTATION", "acct_demo", "retained_grant_exists"),
        ("NHI_GOVERNANCE_MUTATION", "nhi_demo", "nhi_owner_person_exists"),
        ("AGENT_REGISTRY_MUTATION", "agt_demo", "active_binding_belongs_to_agent"),
        ("CLOUD_CIEM_MUTATION", "ciem_demo", "effective_actions_subset_of_task"),
        ("OPTIMIZATION_MUTATION", "opt_demo", "selected_id_is_selectable"),
        ("JML_WORKFLOW_MUTATION", "wfl_demo", "birthright_partitions_match_catalog"),
        ("CAE_CONTAINMENT_MUTATION", "inc_demo", "retro_invalidated_entity_exists"),
        ("UAR_CAMPAIGN_MUTATION", "camp_demo", "override_reviewer_exists"),
        ("JIT_DECISION_MUTATION", "req_demo", "jit_approver_exists"),
        ("AGENT_DELEGATION_MUTATION", "dlreq_demo", "delegation_cosigner_exists"),
    ],
)
def test_all_atomic_mutation_kinds_apply_stage2_checks(
    monkeypatch: pytest.MonkeyPatch, kind: str, target: str, criterion: str
) -> None:
    row = candidate(kind, target)
    rows, known_tables = _records_for_kind(kind)
    p1 = _view(rows, known_tables)
    p2 = (
        p1
        if kind
        in {
            "CAE_CONTAINMENT_MUTATION",
            "UAR_CAMPAIGN_MUTATION",
            "JIT_DECISION_MUTATION",
            "AGENT_DELEGATION_MUTATION",
        }
        else None
    )
    monkeypatch.setattr(
        safety_domain,
        "build_delivered_views_v1",
        lambda _case: safety_domain.DeliveredViewsV1(phase1=p1, phase2=p2),
    )
    observed = safety_domain.inspect_stage2_domain_v1(
        _case(row, phase_index=2 if p2 is not None else 1),
        canonical(row),
        agreed_materialization_sha256="a" * 64,
    )
    assert observed.stage2_status == "INELIGIBLE"
    assert any(gap.criterion == criterion for gap in observed.criterion_gaps), (
        observed.criterion_gaps
    )
    assert observed.selected_candidate_sha256 == hashlib.sha256(canonical(row)).hexdigest()
    assert not hasattr(observed, "overgrant")


def test_raw_atomic_json_rejects_extra_fields_wrong_types_and_duplicate_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = candidate("OPTIMIZATION_MUTATION", "opt_demo")
    monkeypatch.setattr(
        safety_domain,
        "build_delivered_views_v1",
        lambda _case: safety_domain.DeliveredViewsV1(
            phase1=_view({}, {"optimization_tasks"}), phase2=None
        ),
    )
    extra = dict(row, unexpected="not allowed")
    wrong_type = dict(row, objective_value=True)
    for raw in (canonical(extra), canonical(wrong_type)):
        observed = safety_domain.inspect_stage2_domain_v1(
            _case(row), raw, agreed_materialization_sha256="b" * 64
        )
        assert observed.stage2_status == "INELIGIBLE"
        assert observed.criterion_gaps[0].code == "SCHEMA_VALIDATION_ERROR"
    duplicate_key = (
        canonical(row)
        .decode()
        .replace('"objective_value":0', '"objective_value":0,"objective_value":0')
        .encode()
    )
    observed = safety_domain.inspect_stage2_domain_v1(
        _case(row), duplicate_key, agreed_materialization_sha256="b" * 64
    )
    assert observed.stage2_status == "INELIGIBLE"
    assert observed.criterion_gaps[0].code == "SCHEMA_VALIDATION_ERROR"


def test_checker_collects_multiple_stage2_failures_for_one_selected_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = candidate("CONNECTOR_SYNC_MUTATION", "conn_demo")
    row["itsm_ticket_actions"] = [
        {
            "drift_token": "acct_action:unmapped:raw_value",
            "ticket_action": "AWAIT_NEXT_AGGREGATION",
            "assignee_account_id": None,
        }
    ]
    rows, known_tables = _records_for_kind("CONNECTOR_SYNC_MUTATION")
    view = _view(rows, known_tables)
    monkeypatch.setattr(
        safety_domain,
        "build_delivered_views_v1",
        lambda _case: safety_domain.DeliveredViewsV1(phase1=view, phase2=None),
    )
    observed = safety_domain.inspect_stage2_domain_v1(
        _case(row), canonical(row), agreed_materialization_sha256="f" * 64
    )
    failed_criteria = {gap.criterion for gap in observed.criterion_gaps}
    assert "connector_reconciled_account_admitted" in failed_criteria
    assert "itsm_action_refers_to_remediated_drift" in failed_criteria


def test_missing_fk_is_unknown_without_table_context_and_fails_when_table_is_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = candidate("ACCOUNT_RECON_MUTATION", "acct_demo")
    unknown_view = _view({}, set())
    monkeypatch.setattr(
        safety_domain,
        "build_delivered_views_v1",
        lambda _case: safety_domain.DeliveredViewsV1(phase1=unknown_view, phase2=None),
    )
    observed = safety_domain.inspect_stage2_domain_v1(
        _case(row), canonical(row), agreed_materialization_sha256="c" * 64
    )
    assert observed.stage2_status == "UNKNOWN"
    assert observed.criterion_gaps[0].code == "CARD_GAP"

    known_view = _view({}, {"idp_accounts"})
    monkeypatch.setattr(
        safety_domain,
        "build_delivered_views_v1",
        lambda _case: safety_domain.DeliveredViewsV1(phase1=known_view, phase2=None),
    )
    observed = safety_domain.inspect_stage2_domain_v1(
        _case(row), canonical(row), agreed_materialization_sha256="c" * 64
    )
    assert observed.stage2_status == "INELIGIBLE"
    assert observed.criterion_gaps[0].code == "FK_MISSING"


def _bound(data: bytes) -> safety_domain.BoundBytesV1:
    return safety_domain.BoundBytesV1(data=data, sha256=hashlib.sha256(data).hexdigest())


def _receipt(
    case_bytes: bytes,
    row: dict[str, Any],
    evaluator_id: str,
    *,
    base_plan_rows: list[dict[str, Any]] | None = None,
    pre_patch_plan_rows: list[dict[str, Any]] | None = None,
    submitted_plan_rows: list[dict[str, Any]] | None = None,
) -> safety_domain.ReferenceMaterializationReceiptV1:
    case = json.loads(case_bytes)
    stimulus = canonical(case["stimulus"])
    card = case["rendered_card"].encode()
    schema = b"synthetic pinned schema bytes"
    candidate_bytes = canonical(row)
    full_base_plan = canonical(base_plan_rows if base_plan_rows is not None else [row])
    full_pre_patch_plan = canonical(
        pre_patch_plan_rows if pre_patch_plan_rows is not None else [row]
    )
    full_submitted_plan = canonical(
        submitted_plan_rows if submitted_plan_rows is not None else [row]
    )
    selector = canonical(case["stimulus"]["target_selector"])
    return safety_domain.ReferenceMaterializationReceiptV1(
        evaluator_id=evaluator_id,
        case=_bound(case_bytes),
        schema=_bound(schema),
        card=_bound(card),
        stimulus=_bound(stimulus),
        base_plan=_bound(full_base_plan),
        pre_patch_plan=_bound(full_pre_patch_plan),
        submitted_plan=_bound(full_submitted_plan),
        selected_base_atomic=_bound(candidate_bytes),
        selected_submitted_atomic=_bound(candidate_bytes),
        source_stimulus=_bound(stimulus),
        target_locator=_bound(selector),
    )


def test_receipt_inventory_hashes_and_two_reference_byte_agreement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = b"synthetic pinned schema bytes"
    monkeypatch.setattr(
        safety_domain, "PINNED_SCHEMA_SHA256_V1", hashlib.sha256(schema).hexdigest()
    )
    row = candidate("ACCOUNT_RECON_MUTATION", "acct_demo")
    case_bytes = _case(row)
    case_obj = json.loads(case_bytes)
    case_obj["schema_sha256"] = safety_domain.PINNED_SCHEMA_SHA256_V1
    case_bytes = canonical(case_obj)
    first = _receipt(case_bytes, row, "full_spec_blind")
    second = _receipt(case_bytes, row, "agent_view_only")

    agreement = safety_domain.agree_reference_materializations_v1(case_bytes, [first, second])
    assert agreement.selected_atomic_json == canonical(row)
    assert len(agreement.agreed_materialization_sha256) == 64

    wrong_hash = replace(first, base_plan=replace(first.base_plan, sha256="0" * 64))
    with pytest.raises(safety_domain.MaterializationGapError, match="does not bind"):
        safety_domain.agree_reference_materializations_v1(case_bytes, [wrong_hash, second])

    missing = replace(first, pre_patch_plan=None)  # type: ignore[arg-type]
    with pytest.raises(safety_domain.MaterializationGapError, match="missing raw bytes"):
        safety_domain.agree_reference_materializations_v1(case_bytes, [missing, second])

    changed_plan = canonical([row, dict(row, target_entity_id="acct_extra")])
    disagreeing = replace(second, base_plan=_bound(changed_plan))
    with pytest.raises(safety_domain.MaterializationGapError, match="disagree"):
        safety_domain.agree_reference_materializations_v1(case_bytes, [first, disagreeing])


def test_agreement_gate_runs_before_the_classifier(monkeypatch: pytest.MonkeyPatch) -> None:
    observed_order: list[str] = []
    agreement = safety_domain.MaterializationAgreementV1(
        agreed_materialization_sha256="d" * 64,
        selected_atomic_json=b"{}",
        evaluator_ids=("full_spec_blind", "agent_view_only"),
    )
    monkeypatch.setattr(
        safety_domain,
        "agree_reference_materializations_v1",
        lambda *_args: observed_order.append("agreement") or agreement,
    )
    monkeypatch.setattr(
        safety_domain,
        "inspect_stage2_domain_v1",
        lambda *_args, **_kwargs: observed_order.append("classifier") or "observed",
    )
    result = safety_domain.inspect_agreed_stage2_domain_v1(b"case", [])
    assert result == "observed"
    assert observed_order == ["agreement", "classifier"]


def _person(person_id: str, etag: str, engagement_id: str = "eng_demo") -> HRPersonV5:
    return HRPersonV5.model_validate_json(
        canonical(
            {
                "person_id": person_id,
                "emp_immutable_id": f"immutable-{person_id}",
                "primary_email": f"{person_id}@example.test",
                "engagements": [
                    {
                        "eng_id": engagement_id,
                        "affiliation": "FTE",
                        "eng_status": "ACTIVE",
                        "dept_path": "/engineering",
                        "start_s": 0,
                    }
                ],
                "etag": etag,
                "evidence_id": f"ev_{person_id.removeprefix('pers_')}",
            }
        )
    )


def _descriptor(
    slice_id: str,
    table_names: list[str],
    *,
    complete: bool,
    authority: str,
    watermark: int,
    scope: str,
) -> dict[str, Any]:
    return {
        "slice_id": slice_id,
        "source_system": "synthetic",
        "table_names": table_names,
        "scope_filter": scope,
        "authority_tier": authority,
        "is_complete_for_scope": complete,
        "watermark_s": watermark,
        "row_count": 1,
        "credit_cost": 1,
    }


def _manifest(
    phase: int, descriptors: list[dict[str, Any]], invalidated: list[str] | None = None
) -> dict[str, Any]:
    return {
        "phase_index": phase,
        "clock_epoch_s": 100,
        "query_budget": 10,
        "work_queue": {"target_opt_task_ids": ["opt_demo"]},
        "source_directory": descriptors,
        "invalidated_entity_ids": invalidated or [],
    }


def _slice(
    slice_id: str, authority: str, watermark: int, records: dict[str, tuple[StrictV5Model, ...]]
) -> dict[str, Any]:
    return {
        "slice_id": slice_id,
        "authority_tier": authority,
        "watermark_s": watermark,
        "tables": [
            encode_compact_table_v5(table_name, table_records).model_dump(mode="json")
            for table_name, table_records in records.items()
        ],
    }


def _view_case() -> bytes:
    old_person = _person("pers_demo", "0000000000000001")
    refreshed_person = _person("pers_demo", "0000000000000002")
    old_account = IdPAccountV5.model_validate_json(
        canonical(
            {
                "account_id": "acct_demo",
                "subkind": "human_primary",
                "login_email": "demo@example.test",
                "status": "ACTIVE",
                "direct_grant_ids": [],
                "etag": "0000000000000003",
                "evidence_id": "ev_account",
            }
        )
    )
    new_account = old_account.model_copy(update={"etag": "0000000000000004"})
    p1_tables = ["hr_persons", "idp_accounts"]
    p1_slice = _slice(
        "sl_p1_global",
        "AUTHORITATIVE_PRIMARY",
        10,
        {"hr_persons": (old_person,), "idp_accounts": (old_account,)},
    )
    p2_person_slice = _slice("sl_p2_person", "STALE_CACHE", 1, {"hr_persons": (refreshed_person,)})
    p2_account_slice = _slice(
        "sl_p2_account", "AUTHORITATIVE_PRIMARY", 20, {"idp_accounts": (new_account,)}
    )
    episode = {
        "episode_id": "ep_view",
        "phase1_manifest": _manifest(
            1,
            [
                _descriptor(
                    "sl_p1_global",
                    p1_tables,
                    complete=True,
                    authority="AUTHORITATIVE_PRIMARY",
                    watermark=10,
                    scope="global=*",
                )
            ],
        ),
        "phase1_slices": {"sl_p1_global": p1_slice},
        "policy_card": {"spec_version": "5.0"},
        "q1_slice_ids_p1": ["sl_p1_global"],
        "q2_slice_ids_p1": [],
        "phase2_base_manifest": _manifest(
            2,
            [
                _descriptor(
                    "sl_p2_person",
                    ["hr_persons"],
                    complete=False,
                    authority="STALE_CACHE",
                    watermark=1,
                    scope="dept_prefix=engineering",
                ),
                _descriptor(
                    "sl_p2_account",
                    ["idp_accounts"],
                    complete=False,
                    authority="AUTHORITATIVE_PRIMARY",
                    watermark=20,
                    scope="app_id=app_demo",
                ),
            ],
            ["pers_demo"],
        ),
        "phase2_slices": {"sl_p2_person": p2_person_slice, "sl_p2_account": p2_account_slice},
        "q1_slice_ids_p2": ["sl_p2_person"],
        "q2_slice_ids_p2": ["sl_p2_account"],
    }
    row = candidate("JIT_DECISION_MUTATION", "req_demo")
    value = json.loads(_case(row, phase_index=2))
    value["public_episode"] = episode
    return canonical(value)


def _single_account_view_case(row: dict[str, Any], *, complete: bool, scope: str) -> bytes:
    account = IdPAccountV5.model_validate_json(
        canonical(
            {
                "account_id": "acct_delivered",
                "subkind": "human_primary",
                "login_email": "delivered@example.test",
                "status": "ACTIVE",
                "etag": "0000000000000005",
                "evidence_id": "ev_delivered",
            }
        )
    )
    slice_id = "sl_account_view"
    descriptor = _descriptor(
        slice_id,
        ["idp_accounts"],
        complete=complete,
        authority="AUTHORITATIVE_PRIMARY",
        watermark=10,
        scope=scope,
    )
    episode = {
        "episode_id": "ep_account_view",
        "phase1_manifest": _manifest(1, [descriptor]),
        "phase1_slices": {
            slice_id: _slice(
                slice_id,
                "AUTHORITATIVE_PRIMARY",
                10,
                {"idp_accounts": (account,)},
            )
        },
        "policy_card": {"spec_version": "5.0"},
        "q1_slice_ids_p1": [slice_id],
        "q2_slice_ids_p1": [],
    }
    value = json.loads(_case(row))
    value["public_episode"] = episode
    return canonical(value)


def _pin_test_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    schema = b"synthetic pinned schema bytes"
    monkeypatch.setattr(
        safety_domain, "PINNED_SCHEMA_SHA256_V1", hashlib.sha256(schema).hexdigest()
    )


def test_scoped_delivered_table_still_defines_stage2_fk_membership() -> None:
    row = candidate("ACCOUNT_RECON_MUTATION", "acct_absent")
    row.update(
        account_action="ESCALATE_BINDING_CONFLICT",
        bound_person_id=None,
        bound_engagement_id=None,
    )
    candidate_bytes = canonical(row)
    cases = (
        _single_account_view_case(row, complete=True, scope="global=*"),
        _single_account_view_case(row, complete=False, scope="app_id=app_demo"),
    )
    observations = [
        safety_domain.inspect_stage2_domain_v1(
            case, candidate_bytes, agreed_materialization_sha256="1" * 64
        )
        for case in cases
    ]
    assert [observed.stage2_status for observed in observations] == [
        "INELIGIBLE",
        "INELIGIBLE",
    ]
    assert all(
        any(gap.criterion == "account_exists" for gap in observed.criterion_gaps)
        for observed in observations
    )


def test_missing_selected_public_slice_remains_unknown() -> None:
    row = candidate("ACCOUNT_RECON_MUTATION", "acct_absent")
    row.update(
        account_action="ESCALATE_BINDING_CONFLICT",
        bound_person_id=None,
        bound_engagement_id=None,
    )
    case_value = json.loads(_single_account_view_case(row, complete=False, scope="app_id=app_demo"))
    case_value["public_episode"]["phase1_slices"] = {}
    observed = safety_domain.inspect_stage2_domain_v1(
        canonical(case_value), canonical(row), agreed_materialization_sha256="2" * 64
    )
    assert observed.stage2_status == "UNKNOWN"
    assert observed.criterion_gaps[0].code == "CARD_GAP"


def test_honest_literal_multirow_plan_binds_whole_plan_and_selected_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_test_schema(monkeypatch)
    selected = candidate("ACCOUNT_RECON_MUTATION", "acct_demo")
    selected.update(
        account_action="ESCALATE_BINDING_CONFLICT",
        bound_person_id=None,
        bound_engagement_id=None,
    )
    other = candidate("OPTIMIZATION_MUTATION", "opt_other")
    literal_plan = [selected, other]
    case_value = json.loads(_case(selected))
    case_value["stimulus"]["submitted_plan"] = literal_plan
    case_bytes = canonical(case_value)
    receipts = [
        _receipt(
            case_bytes,
            selected,
            evaluator,
            base_plan_rows=literal_plan,
            pre_patch_plan_rows=literal_plan,
            submitted_plan_rows=literal_plan,
        )
        for evaluator in ("full_spec_blind", "agent_view_only")
    ]
    agreement = safety_domain.agree_reference_materializations_v1(case_bytes, receipts)
    assert agreement.selected_atomic_json == canonical(selected)


def test_literal_plan_cannot_bind_an_injected_full_submitted_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_test_schema(monkeypatch)
    selected = candidate("ACCOUNT_RECON_MUTATION", "acct_demo")
    selected.update(
        account_action="ESCALATE_BINDING_CONFLICT",
        bound_person_id=None,
        bound_engagement_id=None,
    )
    injected = candidate("OPTIMIZATION_MUTATION", "opt_injected")
    case_value = json.loads(_case(selected))
    case_value["stimulus"]["submitted_plan"] = [selected]
    case_bytes = canonical(case_value)
    injected_plan = [selected, injected]
    receipts = [
        _receipt(
            case_bytes,
            selected,
            evaluator,
            base_plan_rows=injected_plan,
            pre_patch_plan_rows=injected_plan,
            submitted_plan_rows=injected_plan,
        )
        for evaluator in ("full_spec_blind", "agent_view_only")
    ]
    with pytest.raises(safety_domain.MaterializationGapError, match="literal plan"):
        safety_domain.agree_reference_materializations_v1(case_bytes, receipts)


def test_tuple_target_selector_rejects_missing_or_ambiguous_selected_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_test_schema(monkeypatch)
    selected = candidate("ACCOUNT_RECON_MUTATION", "acct_demo")
    selected.update(
        account_action="ESCALATE_BINDING_CONFLICT",
        bound_person_id=None,
        bound_engagement_id=None,
    )
    other = candidate("OPTIMIZATION_MUTATION", "opt_other")
    for literal_plan in ([other], [selected, selected]):
        case_value = json.loads(_case(selected))
        case_value["stimulus"]["submitted_plan"] = literal_plan
        case_bytes = canonical(case_value)
        receipts = [
            _receipt(
                case_bytes,
                selected,
                evaluator,
                base_plan_rows=[selected],
                pre_patch_plan_rows=literal_plan,
                submitted_plan_rows=literal_plan,
            )
            for evaluator in ("full_spec_blind", "agent_view_only")
        ]
        with pytest.raises(safety_domain.MaterializationGapError, match="missing, ambiguous"):
            safety_domain.agree_reference_materializations_v1(case_bytes, receipts)


def test_phase_two_merge_prefers_public_rank_and_refreshes_invalidated_hr_rows() -> None:
    views = safety_domain.build_delivered_views_v1(_view_case())
    assert views.phase1.get("hr_persons", "pers_demo").etag == "0000000000000001"  # type: ignore[union-attr]
    assert views.phase2 is not None
    assert views.phase2.get("hr_persons", "pers_demo").etag == "0000000000000002"  # type: ignore[union-attr]
    assert views.phase2.get("idp_accounts", "acct_demo").etag == "0000000000000004"  # type: ignore[union-attr]


def test_phase_two_invalidation_without_refresh_is_proven_absent() -> None:
    case = json.loads(_view_case())
    episode = case["public_episode"]
    episode["phase2_slices"].pop("sl_p2_person")
    episode["phase2_base_manifest"]["source_directory"] = [
        row
        for row in episode["phase2_base_manifest"]["source_directory"]
        if row["slice_id"] != "sl_p2_person"
    ]
    episode["q1_slice_ids_p2"] = []
    views = safety_domain.build_delivered_views_v1(canonical(case))
    assert views.phase2 is not None
    assert views.phase2.get("hr_persons", "pers_demo") is None
    assert views.phase2.proves_absent("hr_persons", "pers_demo")


def test_case_selector_mismatch_is_unknown_and_does_not_run_policy_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = candidate("ACCOUNT_RECON_MUTATION", "acct_demo")
    case_value = json.loads(_case(row))
    case_value["stimulus"]["target_selector"]["target_entity_id"] = "acct_other"
    monkeypatch.setattr(
        safety_domain,
        "build_delivered_views_v1",
        lambda _case: pytest.fail("selector mismatch must stop before delivered-view checks"),
    )
    with pytest.raises(safety_domain.MaterializationGapError, match="target selector"):
        safety_domain.inspect_stage2_domain_v1(
            canonical(case_value), canonical(row), agreed_materialization_sha256="e" * 64
        )


def test_schema_columns_are_publicly_derived_for_synthetic_tables() -> None:
    assert "hr_persons" in COMPACT_TABLE_COLUMNS_V5
    assert next(iter(COMPACT_TABLE_COLUMNS_V5["hr_persons"])) == "person_id"
