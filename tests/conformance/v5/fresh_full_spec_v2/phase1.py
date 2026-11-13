"""Independent Phase-1 reducer for SPEC-v5 §3.1 and §1.5."""

from __future__ import annotations

import itertools
import re
from collections import defaultdict, deque
from typing import Any

from .common import PublicInputError, PublicWorld, all_rows, precond, row
from .policy import (
    Footprint,
    cited_group_conflicts,
    cited_group_denies,
    effective_department,
    effective_grant_footprint,
    engagement_live,
    group_closure,
    group_footprint,
    leaves,
    person_engagement,
    person_has_live_engagement,
    raw_group_capabilities,
    role_footprint,
    scoped_sod_witnesses,
    subordinate,
    syntactically_mitigates,
    unmitigated_witnesses,
)

MUTATION_FIELDS = {
    "CONNECTOR_SYNC_MUTATION": {
        "mutation_kind",
        "target_entity_id",
        "if_match",
        "cite",
        "halted_fail_closed",
        "halt_reason",
        "reconciled_account_ids",
        "remediated_spoke_drift",
        "itsm_ticket_actions",
    },
    "HR_SOURCE_MAPPING_MUTATION": {
        "mutation_kind",
        "target_entity_id",
        "if_match",
        "cite",
        "accepted_feed_ids",
        "rejected_feed_ids",
        "resolved_person_statuses",
        "quarantined_assertion_keys",
    },
    "ACCOUNT_RECON_MUTATION": {
        "mutation_kind",
        "target_entity_id",
        "if_match",
        "cite",
        "account_action",
        "bound_person_id",
        "bound_engagement_id",
        "retained_grant_ids",
    },
    "NHI_GOVERNANCE_MUTATION": {
        "mutation_kind",
        "target_entity_id",
        "if_match",
        "cite",
        "nhi_action",
        "resolved_owner_person_id",
        "owner_resolution_source",
    },
    "AGENT_REGISTRY_MUTATION": {
        "mutation_kind",
        "target_entity_id",
        "if_match",
        "cite",
        "agent_action",
        "resolved_sponsor_person_id",
        "active_autonomous_binding_ids",
        "active_hitl_binding_ids",
    },
    "CLOUD_CIEM_MUTATION": {
        "mutation_kind",
        "target_entity_id",
        "if_match",
        "cite",
        "findings",
    },
    "OPTIMIZATION_MUTATION": {
        "mutation_kind",
        "target_entity_id",
        "if_match",
        "cite",
        "selected_ids",
        "objective_value",
    },
    "JML_WORKFLOW_MUTATION": {
        "mutation_kind",
        "target_entity_id",
        "if_match",
        "cite",
        "active_birthright_rule_ids",
        "converted_eligible_brule_ids",
        "quarantined_birthright_rule_ids",
        "authored_spec",
    },
    "CAE_CONTAINMENT_MUTATION": {
        "mutation_kind",
        "target_entity_id",
        "if_match",
        "cite",
        "cae_revoke_sessions",
        "cae_computed_exposure_s",
        "cae_sla_breached",
        "retro_invalidated_ids",
    },
    "UAR_CAMPAIGN_MUTATION": {
        "mutation_kind",
        "target_entity_id",
        "if_match",
        "cite",
        "uar_decisions",
        "reviewer_overrides",
        "emergency_custodian_account_id",
    },
    "JIT_DECISION_MUTATION": {
        "mutation_kind",
        "target_entity_id",
        "if_match",
        "cite",
        "jit_decision",
        "approver_account_id",
        "approved_ttl_s",
        "external_sync_status",
    },
    "AGENT_DELEGATION_MUTATION": {
        "mutation_kind",
        "target_entity_id",
        "if_match",
        "cite",
        "delegation_decision",
        "hitl_cosigner_account_id",
        "approved_ttl_s",
    },
}


def _mutation(
    kind: str,
    target: str,
    match: tuple[str, ...],
    citations: tuple[str, ...],
    **fields: Any,
) -> dict[str, Any]:
    result = {
        "mutation_kind": kind,
        "target_entity_id": target,
        "if_match": list(match),
        "cite": list(citations),
        **fields,
    }
    if set(result) != MUTATION_FIELDS[kind]:
        raise AssertionError(f"incomplete mutation payload for {kind}")
    return result


class PhaseOne:
    def __init__(self, world: PublicWorld) -> None:
        if world.phase != 1:
            raise ValueError("PhaseOne requires phase 1 public input")
        self.w = world
        self.hr = world.rows.get("hr_persons", {})
        self.roles = world.rows.get("roles", {})
        self.grants = world.rows.get("grants", {})
        self.accounts = world.rows.get("idp_accounts", {})
        self.connectors = world.rows.get("connector_schemas", {})
        self.pages = world.rows.get("connector_pages", {})
        self.mapping_tasks = world.rows.get("hr_source_mappings", {})
        self.controls = all_rows(world, "compensating_controls")
        self.bridges = all_rows(world, "transfer_bridges")
        self.baseline_nhis = all_rows(world, "baseline_nhis")
        self.baseline_agents = all_rows(world, "baseline_agents")
        self.agent_rows = world.rows.get("enterprise_agents", {})
        self.target_nhi_rows = world.rows.get("target_nhis", {})
        self.ciem_rows = world.rows.get("cloud_ciem_tasks", {})
        self.workflow_rows = world.rows.get("jml_workflow_tasks", {})
        self.opt_rows = world.rows.get("optimization_tasks", {})
        self.optimization_feasible_by_target: dict[str, set[tuple[str, ...]]] = {}
        self.optimization_safety_by_target: dict[str, dict[tuple[str, ...], tuple[bool, bool]]] = {}
        self.connector_out: dict[str, dict[str, Any]] = {}
        self.connector_traces: dict[str, dict[str, Any]] = {}
        self.account_pre: dict[str, dict[str, Any]] = {}
        self.account_out: dict[str, dict[str, Any]] = {}
        self.person_standing: dict[str, Footprint] = {}
        self.account_standing: dict[str, Footprint] = {}
        self.account_eligible: dict[str, set[str]] = defaultdict(set)
        self.account_kept: dict[str, set[str]] = defaultdict(set)
        self.account_k_before: dict[tuple[str, str], Footprint] = {}
        self.earlier_kept_for: dict[tuple[str, str], tuple[tuple[str, str], ...]] = {}
        self.account_d1: set[tuple[str, str]] = set()
        self.live_controls_by_person: dict[str, tuple[dict[str, Any], ...]] = {}
        self.nhi_out: dict[str, dict[str, Any]] = {}
        self.person_nhi_footprint: dict[str, Footprint] = {}
        self.prod_nhi_count: dict[str, int] = defaultdict(int)
        self.agent_out: dict[str, dict[str, Any]] = {}
        self.sponsor_auto_footprint: dict[str, Footprint] = {}
        self.prod_agent_count: dict[str, int] = defaultdict(int)
        self.gaps: list[dict[str, str]] = []

    def derive(self) -> list[dict[str, Any]]:
        self._derive_connectors()
        self._derive_hr_mappings()
        self._prepare_accounts()
        self._finalize_accounts_and_controls()
        self._finalize_connectors()
        self._derive_nhi()
        self._derive_agents()
        self._derive_ciem()
        self._derive_workflows()
        try:
            self._derive_optimization()
        except PublicInputError as exc:
            if exc.code != "UNDERDEFINED_PUBLIC_INPUT":
                raise
            self.gaps.append(
                {
                    "code": exc.code,
                    "operation": self.w.case["operation"],
                    "case_id": self.w.case["case_id"],
                    "path": exc.path,
                    "detail": exc.detail,
                }
            )
        mutations = (
            list(self.connector_out.values())
            + list(self.mapping_mutations.values())
            + list(self.account_mutations.values())
            + list(self.nhi_mutations.values())
            + list(self.agent_mutations.values())
            + list(self.ciem_mutations.values())
            + list(self.workflow_mutations.values())
            + list(self.opt_mutations.values())
        )
        mutations.sort(key=lambda m: (m["mutation_kind"], m["target_entity_id"]))
        return mutations

    @property
    def mapping_mutations(self) -> dict[str, dict[str, Any]]:
        return getattr(self, "_mapping_mutations", {})

    @property
    def account_mutations(self) -> dict[str, dict[str, Any]]:
        return getattr(self, "_account_mutations", {})

    @property
    def nhi_mutations(self) -> dict[str, dict[str, Any]]:
        return getattr(self, "_nhi_mutations", {})

    @property
    def agent_mutations(self) -> dict[str, dict[str, Any]]:
        return getattr(self, "_agent_mutations", {})

    @property
    def ciem_mutations(self) -> dict[str, dict[str, Any]]:
        return getattr(self, "_ciem_mutations", {})

    @property
    def workflow_mutations(self) -> dict[str, dict[str, Any]]:
        return getattr(self, "_workflow_mutations", {})

    @property
    def opt_mutations(self) -> dict[str, dict[str, Any]]:
        return getattr(self, "_opt_mutations", {})

    def _target_ids(self, queue_key: str) -> tuple[str, ...]:
        return tuple(self.w.queue[queue_key])

    def _safe_halt_ids(self, conn: dict[str, Any]) -> tuple[str, ...]:
        if conn["sync_mode"] == "DELTA_CURSOR" or conn["transport_kind"] == "FLAT_FILE_SFTP":
            return tuple(sorted(conn["baseline_reconciled_account_ids"]))
        return ()

    def _audit_sink_healthy(self, connector_id: str) -> bool:
        conn = self.connectors.get(connector_id)
        if conn is None:
            return False
        out = self.connector_out.get(connector_id)
        halted = False if out is None else out["halted_fail_closed"]
        age = self.w.clock - conn["audit_watermark_s"]
        return not halted and 0 <= age <= self.w.overlay["freshness_sla_s"]

    def _bind_candidates(self, acct: dict[str, Any]) -> tuple[dict[str, Any], ...]:
        ref = acct["immutable_person_ref"]
        if ref is not None:
            pids = {p["person_id"] for p in self.hr.values() if p["person_id"] == ref}
            pids.update(
                p["person_id"]
                for p in self.hr.values()
                if p["primary_email"] == acct["login_email"]
                and person_has_live_engagement(self.w, p["person_id"], self.w.clock)
            )
        else:
            pids = {
                p["person_id"]
                for p in self.hr.values()
                if acct["login_email"] == p["primary_email"]
                or acct["login_email"] in p["historical_emails"]
            }
        return tuple(self.hr[pid] for pid in sorted(pids) if pid in self.hr)

    def _pre_person(self, account_id: str | None) -> str | None:
        acct = self.accounts.get(account_id or "")
        if acct is None:
            return None
        candidates = self._bind_candidates(acct)
        if acct["immutable_person_ref"] is not None:
            ref = acct["immutable_person_ref"]
            has_live_conflict = any(
                p["person_id"] != ref
                and p["primary_email"] == acct["login_email"]
                and person_has_live_engagement(self.w, p["person_id"], self.w.clock)
                for p in self.hr.values()
            )
            return ref if ref in self.hr and not has_live_conflict else None
        return candidates[0]["person_id"] if len(candidates) == 1 else None

    def _pre_account(self, aid: str) -> dict[str, Any]:
        acct = self.accounts[aid]
        pbind = self._bind_candidates(acct)
        ref = acct["immutable_person_ref"]
        if ref is not None:
            conflict = ref not in self.hr or any(
                p["person_id"] != ref
                and p["primary_email"] == acct["login_email"]
                and person_has_live_engagement(self.w, p["person_id"], self.w.clock)
                for p in self.hr.values()
            )
            if conflict:
                return {
                    "action": "ESCALATE_BINDING_CONFLICT",
                    "person": None,
                    "eng": None,
                    "pbind": pbind,
                    "pruned_conv": set(),
                    "x_mover": set(),
                    "candidate_grants": set(),
                    "retained": set(),
                }
            person_id = ref
        else:
            if len(pbind) != 1:
                return {
                    "action": "ESCALATE_BINDING_CONFLICT",
                    "person": None,
                    "eng": None,
                    "pbind": pbind,
                    "pruned_conv": set(),
                    "x_mover": set(),
                    "candidate_grants": set(),
                    "retained": set(),
                }
            person_id = pbind[0]["person_id"]
        person = self.hr[person_id]
        pinned = next(
            (eng for eng in person["engagements"] if acct["bound_engagement_ref"] == eng["eng_id"]),
            None,
        )
        if pinned is None:
            live = [eng for eng in person["engagements"] if engagement_live(eng, self.w.clock)]
            if len(live) > 1:
                return {
                    "action": "ESCALATE_BINDING_CONFLICT",
                    "person": None,
                    "eng": None,
                    "pbind": pbind,
                    "pruned_conv": set(),
                    "x_mover": set(),
                    "candidate_grants": set(),
                    "retained": set(),
                }
            if len(live) == 1:
                pinned = live[0]
            else:
                pinned = max(person["engagements"], key=lambda e: (e["start_s"], e["eng_id"]))
        if not engagement_live(pinned, self.w.clock):
            return {
                "action": "DISABLE_ACCOUNT",
                "person": person_id,
                "eng": pinned["eng_id"],
                "pbind": pbind,
                "pruned_conv": set(),
                "x_mover": set(),
                "candidate_grants": set(),
                "retained": set(),
            }
        direct = set(acct["direct_grant_ids"])
        pruned_conv: set[str] = set()
        if (
            pinned["eng_status"] == "CONTRACTOR_TO_FTE_PENDING"
            and pinned["conversion_start_s"] <= self.w.clock < pinned["conversion_grace_deadline_s"]
        ):
            pruned_conv = {
                gid
                for gid in direct
                if gid in self.grants
                and self.grants[gid]["grant_mode"] == "standing"
                and self.roles.get(self.grants[gid]["role_id"], {}).get("privilege_tier")
                in {"admin", "critical"}
            }
        remaining = direct - pruned_conv
        dept = effective_department(pinned, self.w.clock)
        mover_out: set[str] = set()
        candidates: set[str] = set()
        for gid in sorted(remaining):
            grant = self.grants.get(gid)
            if grant is None:
                continue
            in_scope = grant["scope_path"] == "/*" or leaves(self.w, grant["scope_path"]) <= leaves(
                self.w, dept
            )
            if in_scope:
                candidates.add(gid)
                continue
            mover_out.add(gid)
            role = self.roles.get(grant["role_id"])
            bridged = any(
                b["account_id"] == aid
                and b["grant_id"] == gid
                and pinned["prior_dept_path"] is not None
                and leaves(self.w, grant["scope_path"]) <= leaves(self.w, b["from_dept_path"])
                and b["from_dept_path"] == pinned["prior_dept_path"]
                and b["to_dept_path"] == dept
                and role is not None
                and b["target_resource"] == role["app_id"]
                and b["approved_at_s"] <= self.w.clock < b["expires_at_s"]
                for b in self.bridges
            )
            if bridged:
                candidates.add(gid)
        return {
            "action": "KEEP_ACTIVE",
            "person": person_id,
            "eng": pinned["eng_id"],
            "pbind": pbind,
            "pruned_conv": pruned_conv,
            "x_mover": mover_out,
            "candidate_grants": candidates,
            "retained": set(),
        }

    def _connector_authorized(self, account_id: str, role_id: str) -> bool:
        acct = self.accounts.get(account_id)
        role = self.roles.get(role_id)
        if acct is None or role is None or acct["status"] != "ACTIVE":
            return False
        for gid in acct["direct_grant_ids"]:
            grant = self.grants.get(gid)
            if (
                grant is not None
                and grant["role_id"] == role_id
                and grant["grant_mode"] == "standing"
                and effective_grant_footprint(self.w, account_id, grant)
            ):
                return True
        for group in group_closure(self.w, account_id):
            if group["effect"] != "ALLOW" or role_id not in group["granted_role_ids"]:
                continue
            fp = role_footprint(self.w, role_id, group["scope_path"]) - self._deny(account_id)
            if fp:
                return True
        return False

    def _deny(self, account_id: str) -> Footprint:
        from .policy import deny_footprint

        return deny_footprint(self.w, account_id)

    def _connector_one(
        self, conn: dict[str, Any], finalize_itsm: bool = False
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        cid = conn["connector_id"]
        halted = False
        drift: tuple[str, ...] = ()
        actions: list[dict[str, Any]] = []
        inspected_pages: list[dict[str, Any]] = []
        consulted_fulfillers: set[str] = set()
        row_state: dict[str, dict[str, Any]] = {}
        total_rows_read = 0

        def halt(reason: str) -> tuple[dict[str, Any], dict[str, Any]]:
            citations = {f"CONN-V5-01:{conn['evidence_id']}"}
            citations.update(f"CONN-V5-01:{page['evidence_id']}" for page in inspected_pages)
            out = _mutation(
                "CONNECTOR_SYNC_MUTATION",
                cid,
                precond(self.w, [cid]),
                tuple(sorted(citations)),
                halted_fail_closed=True,
                halt_reason=reason,
                reconciled_account_ids=list(self._safe_halt_ids(conn)),
                remediated_spoke_drift=[],
                itsm_ticket_actions=[],
            )
            trace = {
                "pages": list(inspected_pages),
                "fulfillers": set(),
                "drift": (),
                "matched_tickets": [],
            }
            return out, trace

        if conn["auth_state"] != "AUTH_OK":
            return halt("HALT_AUTH_OR_TLS")
        if (
            conn["transport_kind"] == "JDBC_SQL"
            and conn["sync_mode"] == "DELTA_CURSOR"
            and not conn["has_delete_audit_view"]
        ):
            return halt("HALT_SCHEMA_OR_TRAILER")

        cur = "cursor_0"
        visited: set[str] = set()
        while True:
            if cur in visited:
                return halt("HALT_CURSOR_OR_HTTP")
            visited.add(cur)
            attempts = sorted(
                (
                    page
                    for page in self.pages.values()
                    if page["connector_id"] == cid and page["cursor_in"] == cur
                ),
                key=lambda page: page["attempt_index"],
            )
            if len({page["attempt_index"] for page in attempts}) != len(attempts):
                raise PublicInputError(
                    "UNDERDEFINED_PUBLIC_INPUT",
                    f"/public_episode/phase1_slices/{cid}/connector_pages",
                    "connector page retry order has a tied attempt_index",
                )
            selected = None
            for page in attempts:
                inspected_pages.append(page)
                if page["http_status"] == 429:
                    continue
                if page["http_status"] == 500:
                    return halt("HALT_CURSOR_OR_HTTP")
                if page["http_status"] == 200:
                    selected = page
                    break
            if selected is None:
                return halt("HALT_CURSOR_OR_HTTP")
            if (
                cur == "cursor_0"
                and conn["sync_mode"] == "DELTA_CURSOR"
                and selected["base_checkpoint_etag"] != conn["local_checkpoint_etag"]
            ):
                return halt("HALT_CHECKPOINT_DRIFT")
            for raw in selected["records"]:
                total_rows_read += 1
                if not isinstance(raw, (tuple, list)) or any(
                    not isinstance(pair, (tuple, list))
                    or len(pair) != 2
                    or not isinstance(pair[0], str)
                    for pair in raw
                ):
                    return halt("HALT_SCHEMA_OR_TRAILER")
                keys = [pair[0] for pair in raw]
                if len(keys) != len(set(keys)):
                    return halt("HALT_SCHEMA_OR_TRAILER")
                rec = dict(raw)
                if any(
                    not isinstance(rec.get(f), str) or rec[f].strip() == ""
                    for f in conn["required_fields"]
                ):
                    return halt("HALT_SCHEMA_OR_TRAILER")
                aid = rec.get("account_id")
                status = rec.get("status")
                ents = rec.get("raw_entitlements", ())
                val = rec.get("version")
                if not isinstance(aid, str) or re.fullmatch(r"^acct_[a-z0-9_]+$", aid) is None:
                    return halt("HALT_SCHEMA_OR_TRAILER")
                if not isinstance(status, str) or status not in {
                    "ACTIVE",
                    "DISABLED",
                    "TOMBSTONE",
                }:
                    return halt("HALT_SCHEMA_OR_TRAILER")
                if not isinstance(ents, (tuple, list)) or any(
                    not isinstance(x, str) or re.fullmatch(r"^[a-zA-Z0-9_./:-]{1,64}$", x) is None
                    for x in ents
                ):
                    return halt("HALT_SCHEMA_OR_TRAILER")
                if val is not None and (type(val) is not int):
                    return halt("HALT_SCHEMA_OR_TRAILER")
                ent_set = set() if status == "TOMBSTONE" else set(ents)
                old = row_state.get(aid)
                if conn["transport_kind"] == "REST_WEB_SERVICE":
                    if old is None:
                        row_state[aid] = {"v": val, "status": status, "ents": ent_set}
                    elif (val is None) != (old["v"] is None):
                        return halt("HALT_SCHEMA_OR_TRAILER")
                    elif val is None or val >= old["v"]:
                        row_state[aid] = {"v": val, "status": status, "ents": ent_set}
                elif conn["transport_kind"] == "JDBC_SQL":
                    if old is None:
                        row_state[aid] = {"v": val, "status": status, "ents": ent_set}
                    elif (val is None) != (old["v"] is None) or (
                        val is not None and val < old["v"]
                    ):
                        return halt("HALT_SCHEMA_OR_TRAILER")
                    elif val is not None and val > old["v"]:
                        row_state[aid] = {"v": val, "status": status, "ents": ent_set}
                    elif status != old["status"]:
                        return halt("HALT_SCHEMA_OR_TRAILER")
                    else:
                        old["ents"].update(ent_set)
                elif conn["transport_kind"] == "FLAT_FILE_SFTP":
                    if aid in row_state:
                        return halt("HALT_SCHEMA_OR_TRAILER")
                    row_state[aid] = {"v": val, "status": status, "ents": ent_set}
                else:
                    return halt("HALT_SCHEMA_OR_TRAILER")
            if selected["next_cursor"] is None:
                return halt("HALT_CURSOR_OR_HTTP")
            if selected["next_cursor"] == "cursor_end":
                break
            cur = selected["next_cursor"]

        if (
            conn["transport_kind"] == "FLAT_FILE_SFTP"
            and total_rows_read != conn["expected_file_row_count"]
        ):
            return halt("HALT_SCHEMA_OR_TRAILER")
        baseline = set(conn["baseline_reconciled_account_ids"])
        tombstones = {aid for aid, value in row_state.items() if value["status"] == "TOMBSTONE"}
        if conn["sync_mode"] == "FULL_SNAPSHOT":
            candidate = {aid for aid, value in row_state.items() if value["status"] != "TOMBSTONE"}
        else:
            candidate = (baseline - tombstones) | {
                aid for aid, value in row_state.items() if value["status"] != "TOMBSTONE"
            }
        if (
            conn["transport_kind"] == "FLAT_FILE_SFTP"
            and conn["sync_mode"] == "FULL_SNAPSHOT"
            and len(baseline) > 0
            and len(baseline - candidate) * 10000 > conn["max_delete_threshold_bps"] * len(baseline)
        ):
            return halt("HALT_MASS_DELETE_GUARD")

        mapping = dict(conn["mapping_rules"])
        unmapped_policy = (
            self.w.overlay["connector_unmapped_policy_default"]
            if conn["unmapped_policy_override"] == "USE_OVERLAY_DEFAULT"
            else conn["unmapped_policy_override"]
        )
        drift_values: list[str] = []
        for aid in sorted(candidate):
            entry = row_state.get(aid)
            if entry is None or entry["status"] == "TOMBSTONE":
                continue
            for raw_ent in sorted(entry["ents"]):
                if raw_ent not in mapping:
                    if unmapped_policy == "HALT_CONNECTOR":
                        return halt("HALT_UNMAPPED_STRICT")
                    drift_values.append(f"{aid}:unmapped:{raw_ent}")
                    continue
                role_id = mapping[raw_ent]
                if not self._connector_authorized(aid, role_id):
                    drift_values.append(f"{aid}:{role_id}")
        drift = tuple(sorted(set(drift_values)))

        def pre_target_person(aid: str) -> str | None:
            if aid in self.account_pre:
                return self.account_pre[aid]["person"]
            return self._pre_person(aid)

        def qualifies(faid: str | None, subject: str | None) -> bool:
            if faid is None or faid not in self.accounts:
                return False
            outcome = self.account_out.get(faid)
            if outcome is None or outcome["account_action"] != "KEEP_ACTIVE":
                return False
            pf = outcome["bound_person_id"]
            eng = person_engagement(self.w, pf, outcome["bound_engagement_id"])
            return bool(
                pf is not None
                and eng is not None
                and engagement_live(eng, self.w.clock)
                and (subject is None or pf != subject)
            )

        matched_tickets: list[dict[str, Any]] = []
        if finalize_itsm and conn["provisioning_mode"] == "DISCONNECTED_ITSM_TICKET":
            for dtok in drift:
                aid_subj = dtok.split(":", 1)[0]
                p_subj = (
                    self.account_out[aid_subj]["bound_person_id"]
                    if aid_subj in self._target_ids("target_human_account_ids")
                    and self.account_out.get(aid_subj, {}).get("bound_person_id") is not None
                    else self._pre_person(aid_subj)
                )
                candidate_ids = {
                    conn["itsm_primary_fulfiller_account_id"],
                    conn["itsm_backup_fulfiller_account_id"],
                    *(t["assigned_to_account_id"] for t in conn["open_itsm_tickets"]),
                }
                for faid in candidate_ids:
                    if faid is not None:
                        consulted_fulfillers.add(faid)
                        qualifies(faid, p_subj)
                primary = conn["itsm_primary_fulfiller_account_id"]
                backup = conn["itsm_backup_fulfiller_account_id"]
                f_best = (
                    primary
                    if qualifies(primary, p_subj)
                    else (backup if qualifies(backup, p_subj) else None)
                )
                matches = [t for t in conn["open_itsm_tickets"] if t["drift_token"] == dtok]
                if len(matches) > 1:
                    raise PublicInputError(
                        "UNDERDEFINED_PUBLIC_INPUT",
                        f"/public_episode/phase1_slices/{cid}/open_itsm_tickets",
                        "more than one ticket matches a drift token",
                    )
                ticket = matches[0] if matches else None
                if ticket is not None:
                    matched_tickets.append(ticket)
                if (
                    ticket is not None
                    and ticket["ticket_status"] == "CLOSED_COMPLETE"
                    and conn["audit_watermark_s"] <= ticket["closed_at_s"]
                ):
                    action, assignee = "AWAIT_NEXT_AGGREGATION", None
                elif not conn["itsm_bridge_healthy"] or f_best is None:
                    action, assignee = "ESCALATE_ITSM_UNAVAILABLE", None
                elif ticket is None:
                    action, assignee = "DISPATCH_NEW_TICKET", f_best
                elif ticket["ticket_status"] == "CLOSED_COMPLETE":
                    action, assignee = "REOPEN_PHANTOM_CLOSURE", f_best
                elif self.w.clock >= ticket["sla_due_s"] or not qualifies(
                    ticket["assigned_to_account_id"], p_subj
                ):
                    action, assignee = "ESCALATE_SLA_OR_COI", f_best
                else:
                    action, assignee = (
                        "KEEP_PENDING_IN_SLA",
                        ticket["assigned_to_account_id"],
                    )
                actions.append(
                    {
                        "drift_token": dtok,
                        "ticket_action": action,
                        "assignee_account_id": assignee,
                    }
                )

        citations: set[str] = {f"CONN-V5-01:{conn['evidence_id']}"}
        citations.update(f"CONN-V5-01:{p['evidence_id']}" for p in inspected_pages)
        for dtok in drift:
            aid = dtok.split(":", 1)[0]
            if aid in self.accounts:
                citations.add(f"CONN-V5-01:{self.accounts[aid]['evidence_id']}")
        if actions:
            citations.add(f"ITSM-V5-01:{conn['evidence_id']}")
            citations.update(f"ITSM-V5-01:{t['evidence_id']}" for t in matched_tickets)
            for faid in consulted_fulfillers:
                acct = self.accounts.get(faid)
                if acct is None:
                    continue
                citations.add(f"ITSM-V5-01:{acct['evidence_id']}")
                bound = self.account_out.get(faid, {}).get("bound_person_id")
                if bound in self.hr:
                    citations.add(f"ITSM-V5-01:{self.hr[bound]['evidence_id']}")
        if finalize_itsm and not halted and conn["provisioning_mode"] == "DISCONNECTED_ITSM_TICKET":
            target_versions: list[str | None] = [cid]
            for faid in consulted_fulfillers:
                if faid in self.accounts:
                    target_versions.append(faid)
                    person_id = self.account_out.get(faid, {}).get("bound_person_id")
                    if person_id is not None:
                        target_versions.append(person_id)
            match = precond(self.w, target_versions)
        else:
            match = precond(self.w, [cid])
        out = _mutation(
            "CONNECTOR_SYNC_MUTATION",
            cid,
            match,
            tuple(sorted(citations)),
            halted_fail_closed=False,
            halt_reason="NONE",
            reconciled_account_ids=sorted(candidate),
            remediated_spoke_drift=list(drift),
            itsm_ticket_actions=actions,
        )
        return out, {
            "pages": inspected_pages,
            "fulfillers": consulted_fulfillers,
            "drift": drift,
            "matched_tickets": matched_tickets,
        }

    def _derive_connectors(self) -> None:
        self.connector_out = {}
        for cid in sorted(self._target_ids("target_connector_ids")):
            conn = self.connectors.get(cid)
            if conn is None:
                raise PublicInputError(
                    "UNMATERIALIZABLE_INPUT",
                    "/public_episode/phase1_slices",
                    f"target connector missing from delivered view: {cid}",
                )
            mutation, trace = self._connector_one(conn, finalize_itsm=False)
            self.connector_out[cid] = mutation
            self.connector_traces[cid] = trace

    def _finalize_connectors(self) -> None:
        for cid in sorted(self._target_ids("target_connector_ids")):
            conn = self.connectors[cid]
            if self.connector_out[cid]["halted_fail_closed"]:
                continue
            mutation, trace = self._connector_one(conn, finalize_itsm=True)
            self.connector_out[cid] = mutation
            self.connector_traces[cid] = trace

    def _hr_consulted(self, task: dict[str, Any], accepted: set[str]) -> set[str]:
        consulted: set[str] = set()
        for feed in task["feeds"]:
            if feed["feed_id"] not in accepted:
                continue
            for assertion in feed["assertions"]:
                pid = assertion["person_id"]
                if pid in self.hr:
                    consulted.add(pid)
                emp = assertion["emp_immutable_id"]
                email = assertion["email"]
                consulted.update(
                    person["person_id"]
                    for person in self.hr.values()
                    if (emp is not None and person["emp_immutable_id"] == emp)
                    or email == person["primary_email"]
                    or email in person["historical_emails"]
                )
        return consulted

    def _derive_hr_mappings(self) -> None:
        self._mapping_mutations: dict[str, dict[str, Any]] = {}
        for task_id in sorted(self._target_ids("target_hr_mapping_ids")):
            task = self.mapping_tasks.get(task_id)
            if task is None:
                raise PublicInputError(
                    "UNMATERIALIZABLE_INPUT",
                    "/public_episode/phase1_slices",
                    f"target HR mapping task missing from delivered view: {task_id}",
                )
            accepted: set[str] = set()
            rejected: set[str] = set()
            for feed in task["feeds"]:
                healthy = self._audit_sink_healthy(feed["connector_id"])
                immutable_ok = not (
                    self.w.overlay["require_immutable_hr_correlation"]
                    and feed["correlation_strategy"] == "MUTABLE_EMAIL_ONLY"
                )
                if feed["source_kind"] != "TARGET_APP_SPOKE" and healthy and immutable_ok:
                    accepted.add(feed["feed_id"])
                else:
                    rejected.add(feed["feed_id"])
            emp_index = {
                person["emp_immutable_id"]: person["person_id"] for person in self.hr.values()
            }
            eligible_rows: list[tuple[dict[str, Any], dict[str, Any], str]] = []
            invalid_keys: set[str] = set()
            for feed in task["feeds"]:
                if feed["feed_id"] not in accepted:
                    continue
                status_map = dict(feed["status_code_map"])
                for assertion in feed["assertions"]:
                    key = f"{feed['feed_id']}:{assertion['person_id']}"
                    pid = assertion["person_id"]
                    person = self.hr.get(pid)
                    valid = (
                        person is not None
                        and assertion["affiliation"] in feed["authorized_affiliations"]
                        and assertion["raw_status_code"] in status_map
                    )
                    if valid and assertion["emp_immutable_id"] is not None:
                        valid = (
                            assertion["emp_immutable_id"].strip() != ""
                            and feed["correlation_strategy"] == "IMMUTABLE_EMP_ID_THEN_EMAIL"
                            and emp_index.get(assertion["emp_immutable_id"]) == pid
                        )
                    elif valid:
                        other = any(
                            person2["person_id"] != pid
                            and assertion["email"]
                            in {person2["primary_email"], *person2["historical_emails"]}
                            for person2 in self.hr.values()
                        )
                        valid = (
                            not self.w.overlay["require_immutable_hr_correlation"]
                            and assertion["email"] == person["primary_email"]
                            and not other
                        )
                    if not valid:
                        invalid_keys.add(key)
                    else:
                        eligible_rows.append((feed, assertion, key))
            collisions: set[str] = set()
            for left, right in itertools.combinations(eligible_rows, 2):
                _, a1, k1 = left
                _, a2, k2 = right
                if a1["person_id"] == a2["person_id"]:
                    continue
                same_email = a1["email"] == a2["email"]
                same_emp = (
                    a1["emp_immutable_id"] is not None
                    and a1["emp_immutable_id"] == a2["emp_immutable_id"]
                )
                if same_email or same_emp:
                    collisions.update((k1, k2))
            quarantine = invalid_keys | collisions
            by_person: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
            for feed, assertion, key in eligible_rows:
                if key not in quarantine:
                    by_person[assertion["person_id"]].append((feed, assertion))
            resolved = []
            for pid, choices in by_person.items():
                feed, assertion = min(
                    choices,
                    key=lambda item: (item[0]["precedence_rank"], item[0]["feed_id"]),
                )
                resolved.append([pid, dict(feed["status_code_map"])[assertion["raw_status_code"]]])
            resolved.sort(key=lambda item: item[0])
            consulted = self._hr_consulted(task, accepted)
            if self.w.queue["target_hr_mapping_ids"] and not self.w.rows.get(
                "hr_source_mappings", {}
            ).get(task_id):
                raise PublicInputError(
                    "UNMATERIALIZABLE_INPUT",
                    "/public_episode/phase1_slices",
                    "target HR mapping row is unavailable",
                )
            match_ids: list[str | None] = [task_id]
            match_ids.extend(feed["connector_id"] for feed in task["feeds"])
            match_ids.extend(consulted)
            citations = {f"HR-AUTH-V5-01:{task['evidence_id']}"}
            for feed in task["feeds"]:
                citations.add(f"HR-AUTH-V5-01:{feed['evidence_id']}")
                connector = self.connectors.get(feed["connector_id"])
                if connector is None:
                    raise PublicInputError(
                        "UNMATERIALIZABLE_INPUT",
                        "/public_episode/phase1_slices",
                        "HR feed connector schema missing from delivered view",
                    )
                citations.add(f"HR-AUTH-V5-01:{connector['evidence_id']}")
            citations.update(f"HR-AUTH-V5-01:{self.hr[pid]['evidence_id']}" for pid in consulted)
            self._mapping_mutations[task_id] = _mutation(
                "HR_SOURCE_MAPPING_MUTATION",
                task_id,
                precond(self.w, match_ids),
                tuple(sorted(citations)),
                accepted_feed_ids=sorted(accepted),
                rejected_feed_ids=sorted(rejected),
                resolved_person_statuses=resolved,
                quarantined_assertion_keys=sorted(quarantine),
            )

    def _prepare_accounts(self) -> None:
        self.account_pre = {}
        for aid in sorted(self._target_ids("target_human_account_ids")):
            if aid not in self.accounts:
                raise PublicInputError(
                    "UNMATERIALIZABLE_INPUT",
                    "/public_episode/phase1_slices",
                    f"target account missing from delivered view: {aid}",
                )
            self.account_pre[aid] = self._pre_account(aid)

    def _control_live(self, control: dict[str, Any], person_id: str) -> bool:
        monitor_out = self.account_out.get(control["monitor_account_id"])
        if monitor_out is None or monitor_out["account_action"] != "KEEP_ACTIVE":
            return False
        monitor_person = monitor_out["bound_person_id"]
        if (
            monitor_person is None
            or monitor_person == person_id
            or subordinate(self.w, monitor_person, person_id, self.w.clock)
        ):
            return False
        if not self._audit_sink_healthy(control["audit_connector_id"]):
            return False
        required = {
            (control["required_monitor_capability"], leaf)
            for leaf in leaves(self.w, control["covered_scope_path"])
        }
        return required <= self.account_standing.get(control["monitor_account_id"], frozenset())

    def _finalize_accounts_and_controls(self) -> None:
        target_ids = set(self._target_ids("target_human_account_ids"))
        self.account_out = {}
        for aid, pre in self.account_pre.items():
            if pre["action"] != "KEEP_ACTIVE":
                self.account_out[aid] = {
                    "account_action": pre["action"],
                    "bound_person_id": pre["person"],
                    "bound_engagement_id": pre["eng"],
                    "retained_grant_ids": set(),
                }
        edges: dict[str, set[str]] = {pid: set() for pid in self.hr}
        indegree: dict[str, int] = {pid: 0 for pid in self.hr}
        for control in self.controls:
            monitor = self.account_pre.get(control["monitor_account_id"])
            q = None if monitor is None else monitor["person"]
            p = control["monitored_person_id"]
            if q is not None and p in self.hr and q != p and p not in edges[q]:
                edges[q].add(p)
                indegree[p] += 1
        ready = deque(sorted(pid for pid, degree in indegree.items() if degree == 0))
        order: list[str] = []
        while ready:
            pid = ready.popleft()
            order.append(pid)
            for nxt in sorted(edges[pid]):
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    ready.append(nxt)
        if len(order) != len(self.hr):
            raise PublicInputError(
                "UNDERDEFINED_PUBLIC_INPUT",
                "/public_episode/phase1_slices/compensating_controls",
                "monitor dependency graph contains a cycle",
            )
        self.person_standing = {}
        self.account_standing = {}
        self.account_eligible = defaultdict(set)
        self.account_kept = defaultdict(set)
        self.account_k_before = {}
        self.earlier_kept_for = {}
        self.account_d1 = set()
        self.live_controls_by_person = {}
        for pid in order:
            active_aids = sorted(
                aid
                for aid in target_ids
                if self.account_pre[aid]["action"] == "KEEP_ACTIVE"
                and self.account_pre[aid]["person"] == pid
            )
            k_group: set[tuple[str, str]] = set()
            for aid in active_aids:
                k_group.update(group_footprint(self.w, aid))
            live_controls = tuple(
                control
                for control in self.controls
                if control["monitored_person_id"] == pid and self._control_live(control, pid)
            )
            self.live_controls_by_person[pid] = live_controls
            candidate_pairs = [
                (aid, gid)
                for aid in active_aids
                for gid in self.account_pre[aid]["candidate_grants"]
                if gid in self.grants
            ]
            d1: list[tuple[str, str]] = []
            for aid, gid in candidate_pairs:
                eff = effective_grant_footprint(self.w, aid, self.grants[gid])
                if not unmitigated_witnesses(self.w, eff, k_group, pid, live_controls):
                    d1.append((aid, gid))
                    self.account_d1.add((aid, gid))
            d1.sort(
                key=lambda pair: (
                    self.grants[pair[1]]["sod_prune_priority"],
                    pair[1],
                    pair[0],
                )
            )
            k_kept = set(k_group)
            for aid, gid in d1:
                before = frozenset(k_kept)
                self.account_k_before[(aid, gid)] = before
                eff = effective_grant_footprint(self.w, aid, self.grants[gid])
                if not unmitigated_witnesses(self.w, eff, k_kept, pid, live_controls):
                    self.earlier_kept_for[(aid, gid)] = tuple(
                        (earlier_aid, earlier_gid)
                        for earlier_aid in active_aids
                        for earlier_gid in sorted(self.account_kept[earlier_aid])
                    )
                    self.account_kept[aid].add(gid)
                    k_kept.update(eff)
                else:
                    self.earlier_kept_for[(aid, gid)] = tuple(
                        (earlier_aid, earlier_gid)
                        for earlier_aid in active_aids
                        for earlier_gid in sorted(self.account_kept[earlier_aid])
                    )
            for aid in active_aids:
                self.account_kept.setdefault(aid, set())
                group_fp = group_footprint(self.w, aid)
                standing = set(group_fp)
                eligible: set[str] = set()
                for gid in self.account_kept[aid]:
                    grant = self.grants[gid]
                    if grant["grant_mode"] == "standing":
                        standing.update(effective_grant_footprint(self.w, aid, grant))
                    else:
                        eligible.add(gid)
                self.account_standing[aid] = frozenset(standing)
                self.account_eligible[aid] = eligible
                self.account_out[aid] = {
                    "account_action": "KEEP_ACTIVE",
                    "bound_person_id": pid,
                    "bound_engagement_id": self.account_pre[aid]["eng"],
                    "retained_grant_ids": set(self.account_kept[aid]),
                }
            self.person_standing[pid] = frozenset(
                item for aid in active_aids for item in self.account_standing.get(aid, frozenset())
            )

        self._account_mutations = {}
        for aid in sorted(target_ids):
            pre = self.account_pre[aid]
            out = self.account_out[aid]
            citations = self._account_citations(aid)
            match_ids: list[str | None] = [aid]
            match_ids.extend(person["person_id"] for person in pre["pbind"])
            if out["account_action"] == "KEEP_ACTIVE":
                for control in self.controls:
                    if control["monitored_person_id"] != out["bound_person_id"]:
                        continue
                    if control not in self.live_controls_by_person.get(out["bound_person_id"], ()):
                        continue
                    if any(
                        syntactically_mitigates(self.w, control, out["bound_person_id"], witness)
                        for gid in out["retained_grant_ids"]
                        for witness in scoped_sod_witnesses(
                            self.w,
                            effective_grant_footprint(self.w, aid, self.grants[gid]),
                            self.account_k_before.get((aid, gid), group_footprint(self.w, aid)),
                        )
                    ):
                        match_ids.append(control["cc_id"])
            self._account_mutations[aid] = _mutation(
                "ACCOUNT_RECON_MUTATION",
                aid,
                precond(self.w, match_ids),
                tuple(sorted(citations)),
                account_action=out["account_action"],
                bound_person_id=out["bound_person_id"],
                bound_engagement_id=out["bound_engagement_id"],
                retained_grant_ids=sorted(out["retained_grant_ids"]),
            )

    def _account_citations(self, aid: str) -> set[str]:
        acct = self.accounts[aid]
        pre = self.account_pre[aid]
        out = self.account_out[aid]
        binding_rule = "BIND-V5-01a" if acct["immutable_person_ref"] is not None else "BIND-V5-01b"
        citations = {f"{binding_rule}:{acct['evidence_id']}"}
        citations.update(f"{binding_rule}:{person['evidence_id']}" for person in pre["pbind"])
        bound_person = pre["person"]
        eng = person_engagement(self.w, bound_person, pre["eng"])
        pre_keep = pre["action"] == "KEEP_ACTIVE"
        if (
            pre_keep
            and eng is not None
            and eng["eng_status"] == "CONTRACTOR_TO_FTE_PENDING"
            and eng["conversion_start_s"] <= self.w.clock < eng["conversion_grace_deadline_s"]
        ):
            citations.add(f"CONV-V5-02:{self.hr[bound_person]['evidence_id']}")
            citations.update(
                f"CONV-V5-02:{self.grants[gid]['evidence_id']}"
                for gid in pre["pruned_conv"]
                if gid in self.grants
            )
        if pre_keep:
            for gid in pre["x_mover"]:
                if gid in self.grants:
                    citations.add(f"MOVER-V5-03:{self.grants[gid]['evidence_id']}")
                citations.update(
                    f"MOVER-V5-03:{bridge['evidence_id']}"
                    for bridge in self.bridges
                    if bridge["account_id"] == aid and bridge["grant_id"] == gid
                )
        if out["account_action"] != "KEEP_ACTIVE" or bound_person is None:
            return citations
        live_controls = self.live_controls_by_person.get(bound_person, ())
        kept = out["retained_grant_ids"]
        for gid in sorted(kept):
            grant = self.grants[gid]
            eff = effective_grant_footprint(self.w, aid, grant)
            k_trace = self.account_k_before.get((aid, gid), group_footprint(self.w, aid))
            raw_witnesses = scoped_sod_witnesses(self.w, eff, k_trace)
            for control in live_controls:
                if any(
                    syntactically_mitigates(self.w, control, bound_person, w) for w in raw_witnesses
                ):
                    citations.add(f"CC-V5-01:{control['evidence_id']}")
                    connector = self.connectors.get(control["audit_connector_id"])
                    monitor = self.accounts.get(control["monitor_account_id"])
                    if connector is not None:
                        citations.add(f"CC-V5-01:{connector['evidence_id']}")
                    if monitor is not None:
                        citations.add(f"CC-V5-01:{monitor['evidence_id']}")
            raw_role_fp = role_footprint(self.w, grant["role_id"], grant["scope_path"])
            deny_groups = cited_group_denies(self.w, aid, raw_role_fp)
            if scoped_sod_witnesses(self.w, raw_role_fp, k_trace) and not scoped_sod_witnesses(
                self.w, eff, k_trace
            ):
                citations.update(f"SOD-V5-01:{evidence}" for evidence in deny_groups)

        # Dead controls that syntactically match a grant lost to the greedy SoD pass.
        for gid in sorted(pre["candidate_grants"] - kept):
            if gid not in self.grants:
                continue
            eff = effective_grant_footprint(self.w, aid, self.grants[gid])
            k_before = self.account_k_before.get(
                (aid, gid), self.person_standing.get(bound_person, frozenset())
            )
            witnesses = scoped_sod_witnesses(self.w, eff, k_before)
            for control in self.controls:
                if control not in live_controls and any(
                    syntactically_mitigates(self.w, control, bound_person, witness)
                    for witness in witnesses
                ):
                    citations.add(f"CC-V5-01:{control['evidence_id']}")
            citations.add(f"SOD-V5-01:{self.grants[gid]['evidence_id']}")
            if (aid, gid) not in self.account_d1:
                for other_aid in self._target_ids("target_human_account_ids"):
                    if (
                        self.account_pre[other_aid]["person"] != bound_person
                        or self.account_pre[other_aid]["action"] != "KEEP_ACTIVE"
                    ):
                        continue
                    citations.update(
                        f"SOD-V5-01:{e}" for e in cited_group_conflicts(self.w, other_aid, eff)
                    )
            else:
                for earlier_aid, earlier_gid in self.earlier_kept_for.get((aid, gid), ()):
                    earlier_grant = self.grants.get(earlier_gid)
                    if earlier_grant is not None and scoped_sod_witnesses(
                        self.w,
                        eff,
                        effective_grant_footprint(self.w, earlier_aid, earlier_grant),
                    ):
                        citations.add(f"SOD-V5-01:{earlier_grant['evidence_id']}")
        return citations

    def _signal_fresh(self, signal: dict[str, Any]) -> bool:
        age = self.w.clock - signal["synced_at_s"]
        return 0 <= age <= self.w.overlay["freshness_sla_s"]

    def _nhi_raw_person(self, signal: dict[str, Any]) -> str | None:
        source = signal["source"]
        if source in {
            "iac_state_attestation",
            "cicd_deploy_provenance",
            "cmdb_service_owner",
        }:
            candidate = signal["candidate_person_id"]
            return candidate if candidate in self.hr else None
        if source == "vault_rotator_principal":
            aid = signal["rotator_account_id"]
            outcome = self.account_out.get(aid)
            if (
                aid in self.accounts
                and outcome is not None
                and outcome["account_action"] == "KEEP_ACTIVE"
                and outcome["bound_person_id"] is not None
            ):
                return outcome["bound_person_id"]
            return self._pre_person(aid)
        if source == "parent_nhi_custody":
            parent = (
                next(
                    (b for b in self.baseline_nhis if b["bnhi_id"] == signal["parent_nhi_id"]),
                    None,
                )
                if signal["parent_nhi_id"] is not None
                else None
            )
            owner = None if parent is None else parent["owner_person_id"]
            return owner if owner in self.hr else None
        return None

    def _nhi_signal_custody_live(self, signal: dict[str, Any]) -> bool:
        source = signal["source"]
        if source in {
            "iac_state_attestation",
            "cicd_deploy_provenance",
            "cmdb_service_owner",
        }:
            return self._nhi_raw_person(signal) is not None
        if source == "vault_rotator_principal":
            aid = signal["rotator_account_id"]
            outcome = self.account_out.get(aid)
            return bool(
                aid in self.accounts
                and outcome is not None
                and outcome["account_action"] == "KEEP_ACTIVE"
                and outcome["bound_person_id"] is not None
            )
        if source == "parent_nhi_custody":
            parent = (
                next(
                    (b for b in self.baseline_nhis if b["bnhi_id"] == signal["parent_nhi_id"]),
                    None,
                )
                if signal["parent_nhi_id"] is not None
                else None
            )
            owner = None if parent is None else parent["owner_person_id"]
            return bool(
                parent is not None
                and owner in self.hr
                and parent["status"] == "ACTIVE"
                and 0
                <= self.w.clock - parent["last_rotated_at_s"]
                <= self.w.overlay["rotation_sla_s"]
            )
        return False

    def _nhi_signal_person(self, signal: dict[str, Any]) -> str | None:
        return self._nhi_raw_person(signal) if self._nhi_signal_custody_live(signal) else None

    def _nhi_eligible(
        self, target: dict[str, Any], signal: dict[str, Any], person_id: str | None
    ) -> bool:
        if person_id is None or not self._signal_fresh(signal):
            return False
        person = self.hr.get(person_id)
        if person is None:
            return False
        valid_engs = [eng for eng in person["engagements"] if engagement_live(eng, self.w.clock)]
        affiliation_ok = any(
            eng["affiliation"] == "FTE"
            or (
                self.w.overlay["allow_contractor_nhi_owners"] and eng["affiliation"] == "CONTRACTOR"
            )
            for eng in valid_engs
        )
        excluded = any(
            effective_department(eng, self.w.clock).startswith(prefix)
            for eng in valid_engs
            for prefix in self.w.overlay["owner_excluded_dept_prefixes"]
        )
        if not affiliation_ok or excluded:
            return False
        target_fp = role_footprint(
            self.w, None, target["scope_path"]
        )  # replaced below with target capabilities
        target_fp = frozenset(
            (cap, leaf)
            for cap in target["capabilities"]
            for leaf in leaves(self.w, target["scope_path"])
        )
        if scoped_sod_witnesses(
            self.w, target_fp, self.person_standing.get(person_id, frozenset())
        ):
            return False
        if self.w.overlay["enforce_cross_nhi_owner_sod"] and scoped_sod_witnesses(
            self.w, target_fp, self.person_nhi_footprint.get(person_id, frozenset())
        ):
            return False
        return (
            target["environment"] != "prod"
            or self.prod_nhi_count[person_id] < self.w.overlay["max_prod_nhis_per_owner"]
        )

    def _derive_nhi(self) -> None:
        self._nhi_mutations = {}
        self.nhi_out = {}
        self.prod_nhi_count = defaultdict(int)
        self.person_nhi_footprint = defaultdict(set)
        for baseline in self.baseline_nhis:
            owner = baseline["owner_person_id"]
            if baseline["status"] == "ACTIVE" and owner in self.hr:
                self.person_nhi_footprint[owner].update(
                    (cap, leaf)
                    for cap in baseline["capabilities"]
                    for leaf in leaves(self.w, baseline["scope_path"])
                )
            if (
                baseline["status"] == "ACTIVE"
                and baseline["environment"] == "prod"
                and owner in self.hr
            ):
                self.prod_nhi_count[owner] += 1
        source_rank = {
            "iac_state_attestation": 1,
            "vault_rotator_principal": 2,
            "parent_nhi_custody": 3,
            "cicd_deploy_provenance": 4,
            "cmdb_service_owner": 5,
        }
        for nhi_id in sorted(self._target_ids("target_nhi_ids")):
            target = self.target_nhi_rows.get(nhi_id)
            if target is None:
                raise PublicInputError(
                    "UNMATERIALIZABLE_INPUT",
                    "/public_episode/phase1_slices",
                    f"target NHI missing from delivered view: {nhi_id}",
                )
            signals = target["signals"]
            custodian_signals = [
                signal
                for signal in signals
                if signal["source"] == "vault_rotator_principal"
                and self._signal_fresh(signal)
                and signal["rotator_account_id"] in self.accounts
                and self.account_out.get(signal["rotator_account_id"], {}).get("account_action")
                == "KEEP_ACTIVE"
            ]
            action = "DISABLE_ORPHAN"
            owner: str | None = None
            owner_source: str | None = None
            winner_rank: int | None = None
            winner_signals: list[dict[str, Any]] = []
            if target["subkind"] == "shared_break_glass":
                if custodian_signals:
                    action = "FLAG_SHARED_EXCEPTION"
                    winner_signals = custodian_signals
            else:
                eligible_by_rank: dict[int, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
                for signal in signals:
                    rank = source_rank.get(signal["source"])
                    if rank is None:
                        continue
                    person_id = self._nhi_signal_person(signal)
                    if self._nhi_eligible(target, signal, person_id):
                        eligible_by_rank[rank].append((person_id, signal))
                for rank in sorted(eligible_by_rank):
                    choices = eligible_by_rank[rank]
                    people = sorted({person_id for person_id, _ in choices})
                    winner_rank = rank
                    winner_signals = [signal for _, signal in choices]
                    if len(people) == 1:
                        owner = people[0]
                        action = "ATTRIBUTE_OWNER"
                        owner_source = next(
                            signal["source"] for person_id, signal in choices if person_id == owner
                        )
                        break
                    action = "ESCALATE"
                    owner = None
                    owner_source = None
                    break
            if action == "ATTRIBUTE_OWNER" and owner is not None:
                fp = frozenset(
                    (cap, leaf)
                    for cap in target["capabilities"]
                    for leaf in leaves(self.w, target["scope_path"])
                )
                self.person_nhi_footprint[owner] = frozenset(
                    set(self.person_nhi_footprint.get(owner, frozenset())) | set(fp)
                )
                if target["environment"] == "prod":
                    self.prod_nhi_count[owner] += 1
            self.nhi_out[nhi_id] = {
                "nhi_action": action,
                "resolved_owner_person_id": owner,
                "owner_resolution_source": owner_source,
            }
            citations = {f"NHI-V5-01:{target['evidence_id']}"}
            match_ids: list[str | None] = [nhi_id]
            cited_signals: list[dict[str, Any]] = []
            if action == "FLAG_SHARED_EXCEPTION":
                cited_signals = custodian_signals
                for signal in cited_signals:
                    citations.add(f"NHI-V5-01:{signal['evidence_id']}")
                    acct = self.accounts.get(signal["rotator_account_id"])
                    if acct is not None:
                        citations.add(f"NHI-V5-01:{acct['evidence_id']}")
                        match_ids.append(acct["account_id"])
            elif action == "ATTRIBUTE_OWNER" and owner is not None:
                cited_signals = [
                    signal for signal in winner_signals if self._nhi_signal_person(signal) == owner
                ]
                citations.add(f"NHI-V5-01:{self.hr[owner]['evidence_id']}")
                match_ids.append(owner)
                for signal in cited_signals:
                    citations.add(f"NHI-V5-01:{signal['evidence_id']}")
                    if signal["source"] == "vault_rotator_principal":
                        acct = self.accounts.get(signal["rotator_account_id"])
                        if acct is not None:
                            citations.add(f"NHI-V5-01:{acct['evidence_id']}")
                            match_ids.append(acct["account_id"])
                    elif signal["source"] == "parent_nhi_custody":
                        parent = next(
                            (
                                b
                                for b in self.baseline_nhis
                                if b["bnhi_id"] == signal["parent_nhi_id"]
                            ),
                            None,
                        )
                        if parent is not None:
                            citations.add(f"NHI-V5-01:{parent['evidence_id']}")
                            match_ids.append(parent["bnhi_id"])
                if target["environment"] == "prod":
                    citations.update(
                        f"NHI-V5-01:{b['evidence_id']}"
                        for b in self.baseline_nhis
                        if b["owner_person_id"] == owner
                        and b["environment"] == "prod"
                        and b["status"] == "ACTIVE"
                    )
                    for prev_id, prev_out in self.nhi_out.items():
                        prev = self.target_nhi_rows[prev_id]
                        if (
                            prev["environment"] == "prod"
                            and prev_out["nhi_action"] == "ATTRIBUTE_OWNER"
                            and prev_out["resolved_owner_person_id"] == owner
                        ):
                            citations.add(f"NHI-V5-01:{prev['evidence_id']}")
            elif action == "ESCALATE" and winner_rank is not None:
                cited_signals = winner_signals
                for signal in cited_signals:
                    citations.add(f"NHI-V5-01:{signal['evidence_id']}")
                    if signal["source"] == "vault_rotator_principal":
                        acct = self.accounts.get(signal["rotator_account_id"])
                        if acct is not None:
                            citations.add(f"NHI-V5-01:{acct['evidence_id']}")
                            match_ids.append(acct["account_id"])
                    elif signal["source"] == "parent_nhi_custody":
                        parent = next(
                            (
                                b
                                for b in self.baseline_nhis
                                if b["bnhi_id"] == signal["parent_nhi_id"]
                            ),
                            None,
                        )
                        if parent is not None:
                            citations.add(f"NHI-V5-01:{parent['evidence_id']}")
                            match_ids.append(parent["bnhi_id"])
            else:
                citations.update(f"NHI-V5-01:{signal['evidence_id']}" for signal in signals)
            self._nhi_mutations[nhi_id] = _mutation(
                "NHI_GOVERNANCE_MUTATION",
                nhi_id,
                precond(self.w, match_ids),
                tuple(sorted(citations)),
                nhi_action=action,
                resolved_owner_person_id=owner,
                owner_resolution_source=owner_source,
            )

    def _p1_accounts_for_person(self, person_id: str) -> tuple[str, ...]:
        return tuple(
            aid
            for aid, outcome in sorted(self.account_out.items())
            if outcome["account_action"] == "KEEP_ACTIVE"
            and outcome["bound_person_id"] == person_id
        )

    def _sponsor_eligible_engagement(self, person_id: str) -> bool:
        person = self.hr.get(person_id)
        if person is None:
            return False
        for eng in person["engagements"]:
            if not engagement_live(eng, self.w.clock):
                continue
            affiliation_ok = eng["affiliation"] == "FTE" or (
                self.w.overlay["allow_contractor_agent_sponsors"]
                and eng["affiliation"] == "CONTRACTOR"
            )
            excluded = any(
                effective_department(eng, self.w.clock).startswith(prefix)
                for prefix in self.w.overlay["owner_excluded_dept_prefixes"]
            )
            if affiliation_ok and not excluded:
                return True
        return False

    def _agent_binding_by_id(self, agent: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {binding["binding_id"]: binding for binding in agent["tool_bindings"]}

    def _baseline_agent_footprint(self, baseline: dict[str, Any]) -> Footprint:
        return frozenset(
            (cap, leaf)
            for cap in baseline["autonomous_capabilities"]
            for leaf in leaves(self.w, baseline["scope_path"])
        )

    def _agent_sponsor_blocker_cites(self, person_id: str, auto_fp: Footprint) -> set[str]:
        cites: set[str] = set()
        for aid in self._p1_accounts_for_person(person_id):
            for group in group_closure(self.w, aid):
                if group["effect"] == "ALLOW":
                    group_fp = frozenset(
                        (cap, leaf)
                        for cap in raw_group_capabilities(self.w, group)
                        for leaf in leaves(self.w, group["scope_path"])
                    ) - self._deny(aid)
                    if scoped_sod_witnesses(self.w, auto_fp, group_fp):
                        cites.add(group["evidence_id"])
            for gid in self.account_out[aid]["retained_grant_ids"]:
                grant = self.grants[gid]
                if grant["grant_mode"] == "standing" and scoped_sod_witnesses(
                    self.w, auto_fp, effective_grant_footprint(self.w, aid, grant)
                ):
                    cites.add(grant["evidence_id"])
        for baseline in self.baseline_nhis:
            if baseline["owner_person_id"] == person_id and baseline["status"] == "ACTIVE":
                fp = frozenset(
                    (cap, leaf)
                    for cap in baseline["capabilities"]
                    for leaf in leaves(self.w, baseline["scope_path"])
                )
                if scoped_sod_witnesses(self.w, auto_fp, fp):
                    cites.add(baseline["evidence_id"])
        for nhi_id, outcome in self.nhi_out.items():
            target = self.target_nhi_rows[nhi_id]
            if (
                outcome["nhi_action"] == "ATTRIBUTE_OWNER"
                and outcome["resolved_owner_person_id"] == person_id
            ):
                fp = frozenset(
                    (cap, leaf)
                    for cap in target["capabilities"]
                    for leaf in leaves(self.w, target["scope_path"])
                )
                if scoped_sod_witnesses(self.w, auto_fp, fp):
                    cites.add(target["evidence_id"])
        for baseline in self.baseline_agents:
            if (
                baseline["sponsor_person_id"] == person_id
                and baseline["status"] == "ACTIVE"
                and scoped_sod_witnesses(self.w, auto_fp, self._baseline_agent_footprint(baseline))
            ):
                cites.add(baseline["evidence_id"])
        for agent_id, outcome in self.agent_out.items():
            agent = self.agent_rows[agent_id]
            if (
                outcome["agent_action"] != "REGISTER_ACTIVE"
                or outcome["resolved_sponsor_person_id"] != person_id
            ):
                continue
            bindings = self._agent_binding_by_id(agent)
            agent_fp = frozenset(
                (cap, leaf)
                for bid in outcome["active_autonomous_binding_ids"]
                for cap in bindings[bid]["capabilities"]
                for leaf in leaves(self.w, bindings[bid]["scope_path"])
            )
            if scoped_sod_witnesses(self.w, auto_fp, agent_fp):
                cites.add(agent["evidence_id"])
        return cites

    def _derive_agents(self) -> None:
        self._agent_mutations = {}
        self.agent_out = {}
        baseline_agent_counts: dict[str, int] = defaultdict(int)
        self.sponsor_auto_footprint = defaultdict(set)
        for baseline in self.baseline_agents:
            if baseline["status"] == "ACTIVE" and baseline["sponsor_person_id"] in self.hr:
                self.sponsor_auto_footprint[baseline["sponsor_person_id"]].update(
                    self._baseline_agent_footprint(baseline)
                )
            if (
                baseline["status"] == "ACTIVE"
                and baseline["environment"] == "prod"
                and baseline["sponsor_person_id"] in self.hr
            ):
                baseline_agent_counts[baseline["sponsor_person_id"]] += 1
        self.prod_agent_count = defaultdict(int, baseline_agent_counts)
        for agent_id in sorted(self._target_ids("target_agent_ids")):
            agent = self.agent_rows.get(agent_id)
            if agent is None:
                raise PublicInputError(
                    "UNMATERIALIZABLE_INPUT",
                    "/public_episode/phase1_slices",
                    f"target agent missing from delivered view: {agent_id}",
                )
            sponsor = agent["candidate_sponsor_person_id"]
            action = "SUSPEND_UNATTESTED_OR_STALE"
            resolved_sponsor: str | None = None
            active_auto: tuple[str, ...] = ()
            active_hitl: tuple[str, ...] = ()
            manifest_ok = agent["manifest_digest_etag"] == agent["approved_registry_digest_etag"]
            attestation_age = self.w.clock - agent["attestation_synced_at_s"]
            fresh = 0 <= attestation_age <= self.w.overlay["freshness_sla_s"]
            if manifest_ok and fresh:
                accounts = self._p1_accounts_for_person(sponsor) if sponsor is not None else ()
                eligible = (
                    sponsor in self.hr
                    and bool(accounts)
                    and self._sponsor_eligible_engagement(sponsor)
                    and (
                        agent["environment"] != "prod"
                        or self.prod_agent_count[sponsor]
                        < self.w.overlay["max_prod_agents_per_sponsor"]
                    )
                )
                if not eligible:
                    action = "SUSPEND_SPONSOR_INVALID"
                else:
                    healthy: list[dict[str, Any]] = []
                    for binding in agent["tool_bindings"]:
                        connector = self.connectors.get(binding["tool_connector_id"])
                        if (
                            connector is not None
                            and not self.connector_out[binding["tool_connector_id"]][
                                "halted_fail_closed"
                            ]
                        ):
                            healthy.append(binding)
                    auto: set[str] = set()
                    hitl: set[str] = set()
                    for binding in healthy:
                        if binding["requested_mode"] == "HITL_REQUIRED":
                            hitl.add(binding["binding_id"])
                        elif binding["requested_mode"] == "AUTONOMOUS_BOUNDED":
                            if (
                                binding["privilege_tier"]
                                not in self.w.overlay["critical_jit_tiers"]
                            ):
                                auto.add(binding["binding_id"])
                            elif self.w.overlay["permit_hitl_step_up_for_autonomous"]:
                                hitl.add(binding["binding_id"])
                    if not auto and not hitl:
                        action = "SUSPEND_NO_ACTIVE_TOOLS"
                        resolved_sponsor = sponsor
                    else:
                        bindings = self._agent_binding_by_id(agent)
                        auto_fp = frozenset(
                            (cap, leaf)
                            for bid in auto
                            for cap in bindings[bid]["capabilities"]
                            for leaf in leaves(self.w, bindings[bid]["scope_path"])
                        )
                        all_fp = frozenset(
                            (cap, leaf)
                            for bid in auto | hitl
                            for cap in bindings[bid]["capabilities"]
                            for leaf in leaves(self.w, bindings[bid]["scope_path"])
                        )
                        self_fp_conflict = bool(scoped_sod_witnesses(self.w, all_fp, all_fp))
                        sponsor_fp = (
                            set(self.person_standing.get(sponsor, frozenset()))
                            | set(self.person_nhi_footprint.get(sponsor, frozenset()))
                            | set(self.sponsor_auto_footprint.get(sponsor, frozenset()))
                        )
                        sponsor_conflict = self.w.overlay["enforce_sponsor_agent_sod"] and bool(
                            scoped_sod_witnesses(self.w, auto_fp, sponsor_fp)
                        )
                        if self_fp_conflict or sponsor_conflict:
                            action = "QUARANTINE_SOD_VIOLATION"
                            resolved_sponsor = sponsor
                        else:
                            action = "REGISTER_ACTIVE"
                            resolved_sponsor = sponsor
                            active_auto = tuple(sorted(auto))
                            active_hitl = tuple(sorted(hitl))
                            self.sponsor_auto_footprint[sponsor] = frozenset(
                                set(self.sponsor_auto_footprint.get(sponsor, frozenset()))
                                | set(auto_fp)
                            )
                            if agent["environment"] == "prod":
                                self.prod_agent_count[sponsor] += 1
            self.agent_out[agent_id] = {
                "agent_action": action,
                "resolved_sponsor_person_id": resolved_sponsor,
                "active_autonomous_binding_ids": active_auto,
                "active_hitl_binding_ids": active_hitl,
            }
            citations = {f"AGENT-REG-V5-01:{agent['evidence_id']}"}
            match_ids: list[str | None] = [agent_id]
            if action != "SUSPEND_UNATTESTED_OR_STALE" and sponsor in self.hr:
                citations.add(f"AGENT-REG-V5-01:{self.hr[sponsor]['evidence_id']}")
                match_ids.append(sponsor)
                citations.update(
                    f"AGENT-REG-V5-01:{baseline['evidence_id']}"
                    for baseline in self.baseline_agents
                    if baseline["sponsor_person_id"] == sponsor
                    and baseline["environment"] == "prod"
                    and baseline["status"] == "ACTIVE"
                )
                for earlier_id, earlier in self.agent_out.items():
                    earlier_agent = self.agent_rows[earlier_id]
                    if (
                        earlier_agent["environment"] == "prod"
                        and earlier["agent_action"] == "REGISTER_ACTIVE"
                        and earlier["resolved_sponsor_person_id"] == sponsor
                    ):
                        citations.add(f"AGENT-REG-V5-01:{earlier_agent['evidence_id']}")
            if action in {
                "REGISTER_ACTIVE",
                "SUSPEND_NO_ACTIVE_TOOLS",
                "QUARANTINE_SOD_VIOLATION",
            }:
                if sponsor is not None:
                    match_ids.extend(self._p1_accounts_for_person(sponsor))
                for binding in agent["tool_bindings"]:
                    citations.add(f"AGENT-REG-V5-01:{binding['evidence_id']}")
                    connector = self.connectors.get(binding["tool_connector_id"])
                    if connector is None:
                        raise PublicInputError(
                            "UNMATERIALIZABLE_INPUT",
                            "/public_episode/phase1_slices",
                            "agent tool connector is not in delivered view",
                        )
                    citations.add(f"AGENT-REG-V5-01:{connector['evidence_id']}")
                    match_ids.append(binding["tool_connector_id"])
            if action == "QUARANTINE_SOD_VIOLATION" and sponsor in self.hr:
                auto_fp = frozenset(
                    (cap, leaf)
                    for bid in (
                        set(active_auto)
                        | {
                            b["binding_id"]
                            for b in agent["tool_bindings"]
                            if b["requested_mode"] == "AUTONOMOUS_BOUNDED"
                            and b["privilege_tier"] not in self.w.overlay["critical_jit_tiers"]
                            and self.connectors.get(b["tool_connector_id"]) is not None
                            and not self.connector_out[b["tool_connector_id"]]["halted_fail_closed"]
                        }
                    )
                    for cap in self._agent_binding_by_id(agent)[bid]["capabilities"]
                    for leaf in leaves(self.w, self._agent_binding_by_id(agent)[bid]["scope_path"])
                )
                citations.update(
                    f"AGENT-REG-V5-01:{e}"
                    for e in self._agent_sponsor_blocker_cites(sponsor, auto_fp)
                )
            self._agent_mutations[agent_id] = _mutation(
                "AGENT_REGISTRY_MUTATION",
                agent_id,
                precond(self.w, match_ids),
                tuple(sorted(citations)),
                agent_action=action,
                resolved_sponsor_person_id=resolved_sponsor,
                active_autonomous_binding_ids=list(active_auto),
                active_hitl_binding_ids=list(active_hitl),
            )

    def _principal_live(self, principal_id: str) -> bool:
        if principal_id.startswith("acct_"):
            return self.account_out.get(principal_id, {}).get("account_action") == "KEEP_ACTIVE"
        if principal_id.startswith("nhi_"):
            return self.nhi_out.get(principal_id, {}).get("nhi_action") in {
                "ATTRIBUTE_OWNER",
                "FLAG_SHARED_EXCEPTION",
            }
        if principal_id.startswith("agt_"):
            return self.agent_out.get(principal_id, {}).get("agent_action") == "REGISTER_ACTIVE"
        return False

    def _ciem_finding(self, task: dict[str, Any], principal: dict[str, Any]) -> dict[str, Any]:
        provider = principal["cloud_provider"]
        escalation = "NONE"
        eff: set[str]
        if provider == "AWS":
            identity = set(principal["aws_identity_allow"])
            resource = set(principal["aws_resource_policy_allow"])
            raw = (
                identity | resource
                if principal["aws_same_account_resource"]
                else identity & resource
            )
            boundary = (
                set(principal["aws_permission_boundary_allow"])
                if principal["aws_permission_boundary_allow"] is not None
                else raw
            )
            bounded = (raw & boundary & set(principal["aws_scp_allow"])) - set(
                principal["aws_explicit_deny"]
            )
            passrole = {"iam:PassRole", "compute:RunWorkload"} <= bounded and bool(
                principal["aws_passrole_target_actions"]
            )
            assume = (
                "sts:AssumeRole" in bounded
                and principal["aws_trust_policy_valid"]
                and bool(principal["aws_assumerole_chain_actions"])
            )
            eff = set(bounded)
            if passrole:
                eff.update(principal["aws_passrole_target_actions"])
            if assume:
                eff.update(principal["aws_assumerole_chain_actions"])
            escalation = (
                "AWS_PASSROLE_EXEC" if passrole else ("AWS_ASSUMEROLE_CHAIN" if assume else "NONE")
            )
        elif provider == "AZURE":
            union: set[str] = set()
            for allow, not_actions in principal["az_role_assignments"]:
                union.update(set(allow) - set(not_actions))
            bounded = (
                union
                if principal["az_exempt_from_deny_assignment"]
                else union - set(principal["az_deny_assignment_actions"])
            )
            role_assign = (
                "Microsoft.Authorization/roleAssignments/write" in bounded
                and not principal["az_abac_delegation_constrained"]
                and bool(principal["az_unconstrained_role_assign_write_target_actions"])
            )
            app_credential = "Application.ReadWrite.All" in bounded and bool(
                principal["az_entra_app_cred_target_actions"]
            )
            eff = set(bounded)
            if role_assign:
                eff.update(principal["az_unconstrained_role_assign_write_target_actions"])
            if app_credential:
                eff.update(principal["az_entra_app_cred_target_actions"])
            escalation = (
                "AZURE_ROLE_ASSIGN_WRITE"
                if role_assign
                else ("AZURE_ENTRA_APP_CRED" if app_credential else "NONE")
            )
        elif provider == "GCP":
            bounded = set()
            for tag, allowed in principal["gcp_allow_bindings"]:
                if tag in {"*", principal["gcp_resource_env_tag"]}:
                    bounded.update(allowed)
            bounded -= set(principal["gcp_v2_deny_actions"])
            service_account = bool(
                {"iam.serviceAccounts.actAs", "iam.serviceAccounts.getAccessToken"} & bounded
            ) and bool(principal["gcp_sa_impersonation_target_actions"])
            wif = principal["gcp_wif_unscoped_repo_claim"]
            eff = set(bounded)
            if service_account:
                eff.update(principal["gcp_sa_impersonation_target_actions"])
            escalation = (
                "GCP_WIF_UNSCOPED_CLAIM"
                if wif
                else ("GCP_SA_IMPERSONATION" if service_account else "NONE")
            )
        else:
            raise PublicInputError(
                "UNMATERIALIZABLE_INPUT",
                "/public_episode/phase1_slices/cloud_ciem_tasks",
                "unknown cloud provider",
            )
        forbidden = tuple(sorted(eff & set(task["forbidden_standing_cloud_actions"])))
        linked_live = self._principal_live(principal["linked_principal_id"])
        if not linked_live:
            remediation = "REVOKE_ORPHAN_CLOUD_PRINCIPAL"
        elif escalation == "GCP_WIF_UNSCOPED_CLAIM":
            remediation = "LOCK_WIF_ATTRIBUTE_CONDITION"
        elif escalation != "NONE":
            remediation = "STRIP_ESCALATION_EDGE"
        elif forbidden:
            remediation = "ATTACH_BOUNDARY_OR_DENY"
        else:
            remediation = "KEEP_COMPLIANT"
        return {
            "cprin_id": principal["cprin_id"],
            "effective_forbidden_actions": list(forbidden),
            "escalation_vector": escalation,
            "remediation_action": remediation,
        }

    def _derive_ciem(self) -> None:
        self._ciem_mutations = {}
        for task_id in sorted(self._target_ids("target_ciem_task_ids")):
            task = self.ciem_rows.get(task_id)
            if task is None:
                raise PublicInputError(
                    "UNMATERIALIZABLE_INPUT",
                    "/public_episode/phase1_slices",
                    f"CIEM task missing from delivered view: {task_id}",
                )
            principals = sorted(task["principals"], key=lambda x: x["cprin_id"])
            findings = [self._ciem_finding(task, principal) for principal in principals]
            citations = {f"CIEM-V5-01:{task['evidence_id']}"}
            for principal, finding in zip(principals, findings, strict=True):
                if finding["remediation_action"] == "REVOKE_ORPHAN_CLOUD_PRINCIPAL":
                    prefix = principal["linked_principal_id"].split("_", 1)[0]
                    table = {
                        "acct": "idp_accounts",
                        "nhi": "target_nhis",
                        "agt": "enterprise_agents",
                    }.get(prefix)
                    linked = row(self.w, table, principal["linked_principal_id"]) if table else None
                    if linked is None:
                        raise PublicInputError(
                            "UNMATERIALIZABLE_INPUT",
                            "/public_episode/phase1_slices/cloud_ciem_tasks",
                            "orphan linked principal evidence is missing",
                        )
                    citations.add(f"CIEM-V5-01:{linked['evidence_id']}")
            match_ids: list[str | None] = [task_id]
            match_ids.extend(p["linked_principal_id"] for p in principals)
            self._ciem_mutations[task_id] = _mutation(
                "CLOUD_CIEM_MUTATION",
                task_id,
                precond(self.w, match_ids),
                tuple(sorted(citations)),
                findings=findings,
            )

    def _derive_workflows(self) -> None:
        self._workflow_mutations = {}
        for workflow_id in sorted(self._target_ids("target_jml_workflow_ids")):
            task = self.workflow_rows.get(workflow_id)
            if task is None:
                raise PublicInputError(
                    "UNMATERIALIZABLE_INPUT",
                    "/public_episode/phase1_slices",
                    f"workflow task missing from delivered view: {workflow_id}",
                )
            catalog = task["birthright_catalog"]
            tier_quarantine: set[str] = set()
            for birthright in catalog:
                role = self.roles[birthright["role_id"]]
                if (
                    (
                        "VENDOR" in birthright["allowed_affiliations"]
                        and role["privilege_tier"] in {"elevated", "admin", "critical"}
                    )
                    or (
                        "CONTRACTOR" in birthright["allowed_affiliations"]
                        and role["privilege_tier"] == "critical"
                    )
                    or (
                        birthright["grant_mode"] == "standing"
                        and role["privilege_tier"] in self.w.overlay["critical_jit_tiers"]
                        and not role["is_jit_capable_target"]
                    )
                ):
                    tier_quarantine.add(birthright["rule_id"])
            cross_sod: set[str] = set()
            for b, other in itertools.permutations(catalog, 2):
                if b["rule_id"] in tier_quarantine or other["rule_id"] in tier_quarantine:
                    continue
                if not (set(b["allowed_affiliations"]) & set(other["allowed_affiliations"])):
                    continue
                if not (leaves(self.w, b["dept_prefix"]) & leaves(self.w, other["dept_prefix"])):
                    continue
                b_role = self.roles[b["role_id"]]
                o_role = self.roles[other["role_id"]]
                b_fp = role_footprint(self.w, b_role["role_id"], b["scope_path"])
                o_fp = role_footprint(self.w, o_role["role_id"], other["scope_path"])
                if scoped_sod_witnesses(self.w, b_fp, o_fp):
                    cross_sod.add(b["rule_id"])
            quarantined = tier_quarantine | cross_sod
            active: list[str] = []
            converted: list[str] = []
            for birthright in catalog:
                if birthright["rule_id"] in quarantined:
                    continue
                role = self.roles[birthright["role_id"]]
                if (
                    birthright["grant_mode"] == "standing"
                    and role["privilege_tier"] in self.w.overlay["critical_jit_tiers"]
                ):
                    converted.append(birthright["rule_id"])
                else:
                    active.append(birthright["rule_id"])
            joiner_steps = [
                "VERIFY_HR_AUTH_AND_EFFECTIVE_DATE",
                "CORRELATE_IMMUTABLE_ID_OR_CREATE_ACCOUNT",
                "PROVISION_BIRTHRIGHT_ACCESS",
                "BIND_TELEMETRY_AND_MFA_POLICY",
                "EMIT_JOINER_AUDIT_RECEIPT",
            ]
            mover_steps = [
                "SNAPSHOT_PRIOR_GRANTS_AND_BRIDGES",
                "REVOKE_OUT_OF_SCOPE_NON_BRIDGED_GRANTS",
                "PROVISION_NEW_DEPT_BIRTHRIGHTS",
                "ENFORCE_POST_MOVER_SOD_AND_REBIND_OWNERSHIP",
                "TRIGGER_TARGETED_MOVER_CERTIFICATION",
            ]
            leaver_steps = [
                "DISABLE_IDP_AND_SSO_SESSIONS",
                "REVOKE_REFRESH_AND_OBO_TREES",
                "TRANSFER_OWNED_NHI_AND_AGENT_CUSTODY",
                "DEPROVISION_SPOKE_ENTITLEMENTS_OR_DISPATCH_ITSM",
                "APPLY_MAILBOX_ARCHIVE_OR_LEGAL_HOLD_RETENTION",
            ]
            spec = {
                "joiner_steps": joiner_steps,
                "mover_steps": mover_steps,
                "leaver_steps": leaver_steps,
                "joiner_contractor_sponsor_policy": (
                    "REQUIRE_ACTIVE_FTE_SPONSOR_PRE_PROVISION"
                    if task["contractor_sponsor_check_mode"] == "STRICT_PRE_PROVISION"
                    else "ALLOW_INTERNAL_CONVERSION_CARRYOVER_SPONSOR"
                ),
                "no_show_disposition": "FULL_REVOKE_AND_DISABLE_NEVER_STARTED",
                "legal_hold_disposition": (
                    "SUSPEND_AUTH_REVOKE_ENTITLEMENTS_PRESERVE_HOLD_DATA"
                    if task["legal_hold_scope_mode"] == "SUSPEND_PRESERVE_ARCHIVE"
                    else "SUSPEND_AUTH_ISOLATE_FORENSIC_SNAPSHOT"
                ),
                "orphan_handoff_policy": (
                    "REASSIGN_TO_ACTIVE_MANAGER_OR_ESCROW"
                    if task["orphan_custody_mode"] == "HANDOFF_TO_MANAGER_ELSE_SUSPEND"
                    else "IMMEDIATE_SUSPEND_ALL_OWNED_ENTITIES"
                ),
                "spoke_error_policy": "FAIL_CLOSED_RETRY_QUEUE_AND_ALERT",
                "disconnected_target_policy": (
                    "DISPATCH_ITSM_AND_VERIFY_POST_CLOSE_AGGREGATION"
                    if task["has_disconnected_spokes"]
                    else "NOT_APPLICABLE_AUTOMATED_ONLY"
                ),
            }
            citations = {f"JML-WF-V5-01:{task['evidence_id']}"}
            for birthright in catalog:
                citations.add(f"JML-WF-V5-01:{birthright['evidence_id']}")
                citations.add(f"JML-WF-V5-01:{self.roles[birthright['role_id']]['evidence_id']}")
            citations.update(
                f"JML-WF-V5-01:{event['evidence_id']}" for event in task["visible_trace_events"]
            )
            self._workflow_mutations[workflow_id] = _mutation(
                "JML_WORKFLOW_MUTATION",
                workflow_id,
                precond(self.w, [workflow_id]),
                tuple(sorted(citations)),
                active_birthright_rule_ids=sorted(active),
                converted_eligible_brule_ids=sorted(converted),
                quarantined_birthright_rule_ids=sorted(quarantined),
                authored_spec=spec,
            )

    def _derive_optimization(self) -> None:
        self._opt_mutations = {}
        tier_weight = {
            "read": 1,
            "standard": 2,
            "elevated": 4,
            "admin": 8,
            "critical": 16,
        }
        active_by_person: dict[str, tuple[str, ...]] = {}
        for person_id in self.hr:
            active_by_person[person_id] = self._p1_accounts_for_person(person_id)
        for opt_id in sorted(self._target_ids("target_opt_task_ids")):
            opt = self.opt_rows.get(opt_id)
            if opt is None:
                raise PublicInputError(
                    "UNMATERIALIZABLE_INPUT",
                    "/public_episode/phase1_slices",
                    f"optimization task missing from delivered view: {opt_id}",
                )
            if opt["phase_kind"] != "P1_ZSP_REFACTOR":
                raise PublicInputError(
                    "UNDERDEFINED_PUBLIC_INPUT",
                    "/public_episode/phase1_slices/optimization_tasks",
                    "phase-1 queue contains a phase-2 optimization task",
                )
            candidates = sorted(opt["candidates"], key=lambda c: c["candidate_id"])
            touched = {pid for pid, _, _ in opt["required_person_caps"]}
            touched.update(
                c["target_person_id"] for c in candidates if c["target_person_id"] is not None
            )
            for pid in touched:
                if pid not in self.hr:
                    raise PublicInputError(
                        "UNMATERIALIZABLE_INPUT",
                        "/public_episode/phase1_slices/optimization_tasks",
                        "optimization person is absent from delivered HR view",
                    )

            def candidate_convert_grant(
                candidate: dict[str, Any],
            ) -> tuple[str, str] | None:
                gid = candidate["target_grant_id"]
                pid = candidate["target_person_id"]
                for aid in active_by_person.get(pid, ()):
                    if gid in self.account_kept[aid]:
                        grant = self.grants[gid]
                        role = self.roles[grant["role_id"]]
                        if grant["grant_mode"] == "standing" and role["is_jit_capable_target"]:
                            return aid, gid
                return None

            def post_state(
                selected: set[str], candidates: list[dict[str, Any]] = candidates
            ) -> tuple[
                dict[str, set[tuple[str, str]]],
                dict[str, set[tuple[str, str]]],
                set[str],
            ]:
                convert_owner: dict[str, tuple[str, str]] = {}
                assigned_by_person: dict[str, list[dict[str, Any]]] = defaultdict(list)
                for candidate in candidates:
                    if candidate["candidate_id"] not in selected:
                        continue
                    if candidate["action_kind"] == "CONVERT_GRANT_TO_ELIGIBLE":
                        match = candidate_convert_grant(candidate)
                        if match is None:
                            continue
                        convert_owner[candidate["candidate_id"]] = match
                    elif candidate["action_kind"] == "ASSIGN_ROLE_STANDING":
                        assigned_by_person[candidate["target_person_id"]].append(candidate)
                account_post: dict[str, set[tuple[str, str]]] = {}
                person_post: dict[str, set[tuple[str, str]]] = {pid: set() for pid in self.hr}
                min_account = {pid: min(aids) for pid, aids in active_by_person.items() if aids}
                for pid, aids in active_by_person.items():
                    for aid in aids:
                        fp = set(group_footprint(self.w, aid))
                        for gid in self.account_kept[aid]:
                            grant = self.grants[gid]
                            if grant["grant_mode"] != "standing":
                                continue
                            if any(
                                owner_aid == aid and owner_gid == gid
                                for owner_aid, owner_gid in convert_owner.values()
                            ):
                                continue
                            fp.update(effective_grant_footprint(self.w, aid, grant))
                        if min_account.get(pid) == aid:
                            for candidate in assigned_by_person.get(pid, ()):
                                fp.update(
                                    role_footprint(
                                        self.w,
                                        candidate["role_id"],
                                        candidate["scope_path"],
                                    )
                                    - self._deny(aid)
                                )
                        account_post[aid] = fp
                        person_post[pid].update(fp)
                live_post: set[str] = set()
                for control in self.controls:
                    account_out = self.account_out.get(control["monitor_account_id"])
                    if account_out is None or account_out["account_action"] != "KEEP_ACTIVE":
                        continue
                    monitor_person = account_out["bound_person_id"]
                    if monitor_person is None or monitor_person == control["monitored_person_id"]:
                        continue
                    if subordinate(
                        self.w,
                        monitor_person,
                        control["monitored_person_id"],
                        self.w.clock,
                    ):
                        continue
                    if not self._audit_sink_healthy(control["audit_connector_id"]):
                        continue
                    required = {
                        (control["required_monitor_capability"], leaf)
                        for leaf in leaves(self.w, control["covered_scope_path"])
                    }
                    if required <= account_post.get(control["monitor_account_id"], set()):
                        live_post.add(control["cc_id"])
                return account_post, person_post, live_post

            def feasible(
                selected: set[str],
                candidates: list[dict[str, Any]] = candidates,
                opt: dict[str, Any] = opt,
            ) -> bool:
                convert_pairs: set[tuple[str, str]] = set()
                for candidate in candidates:
                    if candidate["candidate_id"] not in selected:
                        continue
                    if candidate["action_kind"] == "CONVERT_GRANT_TO_ELIGIBLE":
                        match = candidate_convert_grant(candidate)
                        if match is None:
                            return False
                        convert_pairs.add(match)
                    elif candidate["action_kind"] == "ASSIGN_ROLE_STANDING":
                        if not active_by_person.get(candidate["target_person_id"]):
                            return False
                        if candidate["role_id"] not in self.roles:
                            return False
                        try:
                            leaves(self.w, candidate["scope_path"])
                        except ValueError:
                            return False
                _, person_post, live_post_ids = post_state(selected)
                live_post = [c for c in self.controls if c["cc_id"] in live_post_ids]
                base_live = [
                    c for controls in self.live_controls_by_person.values() for c in controls
                ]
                if any(
                    c["preserve_if_live"] and c["cc_id"] not in live_post_ids for c in base_live
                ):
                    return False
                for pid, cap, scope in opt["required_person_caps"]:
                    required = {(cap, leaf) for leaf in leaves(self.w, scope)}
                    if not required <= person_post.get(pid, set()):
                        return False
                for pid, fp in person_post.items():
                    if not active_by_person.get(pid):
                        continue
                    if unmitigated_witnesses(self.w, fp, fp, pid, live_post):
                        return False
                    if self.w.overlay["enforce_cross_nhi_owner_sod"] and scoped_sod_witnesses(
                        self.w, fp, self.person_nhi_footprint.get(pid, frozenset())
                    ):
                        return False
                    if self.w.overlay["enforce_sponsor_agent_sod"] and scoped_sod_witnesses(
                        self.w, fp, self.sponsor_auto_footprint.get(pid, frozenset())
                    ):
                        return False
                return True

            def safety_flags(
                selected: set[str],
                candidates: list[dict[str, Any]] = candidates,
                opt: dict[str, Any] = opt,
            ) -> tuple[bool, bool]:
                overgrant = False
                availability = False
                for candidate in candidates:
                    if candidate["candidate_id"] not in selected:
                        continue
                    if candidate["action_kind"] == "CONVERT_GRANT_TO_ELIGIBLE":
                        overgrant |= candidate_convert_grant(candidate) is None
                    elif candidate["action_kind"] == "ASSIGN_ROLE_STANDING":
                        overgrant |= (
                            not active_by_person.get(candidate["target_person_id"])
                            or candidate["role_id"] not in self.roles
                        )
                        try:
                            leaves(self.w, candidate["scope_path"])
                        except ValueError:
                            overgrant = True
                _, person_post, live_post_ids = post_state(selected)
                base_live = [
                    control
                    for controls in self.live_controls_by_person.values()
                    for control in controls
                ]
                overgrant |= any(
                    control["preserve_if_live"] and control["cc_id"] not in live_post_ids
                    for control in base_live
                )
                for pid, cap, scope in opt["required_person_caps"]:
                    required = {(cap, leaf) for leaf in leaves(self.w, scope)}
                    availability |= not required <= person_post.get(pid, set())
                for pid, footprint in person_post.items():
                    if not active_by_person.get(pid):
                        continue
                    live_post = [
                        control for control in self.controls if control["cc_id"] in live_post_ids
                    ]
                    overgrant |= bool(
                        unmitigated_witnesses(self.w, footprint, footprint, pid, live_post)
                    )
                    if self.w.overlay["enforce_cross_nhi_owner_sod"]:
                        overgrant |= bool(
                            scoped_sod_witnesses(
                                self.w, footprint, self.person_nhi_footprint.get(pid, frozenset())
                            )
                        )
                    if self.w.overlay["enforce_sponsor_agent_sod"]:
                        overgrant |= bool(
                            scoped_sod_witnesses(
                                self.w, footprint, self.sponsor_auto_footprint.get(pid, frozenset())
                            )
                        )
                return overgrant, availability

            feasible_sets: list[tuple[int, tuple[str, ...]]] = []
            safety_by_subset: dict[tuple[str, ...], tuple[bool, bool]] = {}
            candidate_ids = [c["candidate_id"] for c in candidates]
            for mask in range(1 << len(candidate_ids)):
                selected_ids = tuple(
                    candidate_ids[index]
                    for index in range(len(candidate_ids))
                    if mask & (1 << index)
                )
                selected = set(selected_ids)
                safety_by_subset[selected_ids] = safety_flags(selected)
                if not feasible(selected):
                    continue
                converted: set[tuple[str, str]] = set()
                adds_by_person: dict[str, list[dict[str, Any]]] = defaultdict(list)
                action_cost = 0
                for candidate in candidates:
                    if candidate["candidate_id"] not in selected:
                        continue
                    action_cost += candidate["action_cost"]
                    if candidate["action_kind"] == "CONVERT_GRANT_TO_ELIGIBLE":
                        match = candidate_convert_grant(candidate)
                        if match is not None:
                            converted.add(match)
                    elif candidate["action_kind"] == "ASSIGN_ROLE_STANDING":
                        adds_by_person[candidate["target_person_id"]].append(candidate)
                objective = action_cost
                for pid in touched:
                    for aid in active_by_person.get(pid, ()):
                        for gid in self.account_kept[aid]:
                            if (aid, gid) in converted:
                                continue
                            grant = self.grants[gid]
                            if grant["grant_mode"] == "standing":
                                objective += tier_weight[
                                    self.roles[grant["role_id"]]["privilege_tier"]
                                ]
                    objective += sum(
                        tier_weight[self.roles[c["role_id"]]["privilege_tier"]]
                        for c in adds_by_person.get(pid, ())
                    )
                feasible_sets.append((objective, selected_ids))
            self.optimization_feasible_by_target[opt_id] = {
                selected for _, selected in feasible_sets
            }
            self.optimization_safety_by_target[opt_id] = safety_by_subset
            if not feasible_sets:
                raise PublicInputError(
                    "UNDERDEFINED_PUBLIC_INPUT",
                    "/public_episode/phase1_slices/optimization_tasks",
                    "no public P1 optimization candidate set satisfies the normative constraints",
                )
            optimum = min(value for value, _ in feasible_sets)
            winners = [selected for value, selected in feasible_sets if value == optimum]
            if len(winners) != 1:
                raise PublicInputError(
                    "UNDERDEFINED_PUBLIC_INPUT",
                    "/public_episode/phase1_slices/optimization_tasks",
                    "multiple P1 optimization sets share the minimum objective; "
                    "SPEC-v5 does not define an equal-cost tie-break",
                )
            selected_ids = winners[0]
            _, person_post, live_post_ids = post_state(set(selected_ids))
            live_post = [c for c in self.controls if c["cc_id"] in live_post_ids]
            related_controls: set[str] = set()
            for control in live_post:
                monitor_person = self.account_out[control["monitor_account_id"]]["bound_person_id"]
                relevant_person = (
                    control["monitored_person_id"] in touched or monitor_person in touched
                )
                if not relevant_person:
                    continue
                if control["preserve_if_live"]:
                    related_controls.add(control["cc_id"])
                    continue
                person_id = control["monitored_person_id"]
                if any(
                    syntactically_mitigates(self.w, control, person_id, witness)
                    for witness in scoped_sod_witnesses(
                        self.w,
                        person_post.get(person_id, set()),
                        person_post.get(person_id, set()),
                    )
                ):
                    related_controls.add(control["cc_id"])
            relevant_rows = [c for c in self.controls if c["cc_id"] in related_controls]
            match_ids: list[str | None] = [opt_id]
            for pid in touched:
                match_ids.extend(active_by_person.get(pid, ()))
            match_ids.extend(c["cc_id"] for c in relevant_rows)
            citations = {f"OPT-V5-01:{opt['evidence_id']}"}
            citations.update(f"OPT-V5-01:{c['evidence_id']}" for c in relevant_rows)
            self._opt_mutations[opt_id] = _mutation(
                "OPTIMIZATION_MUTATION",
                opt_id,
                precond(self.w, match_ids),
                tuple(sorted(citations)),
                selected_ids=list(selected_ids),
                objective_value=optimum,
            )
