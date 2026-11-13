"""Contract tests for the isolated delivered-card reference exporter."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from tests.conformance.v5.agent_view_reference import (
    CARD_SHA256,
    DELIVERED_PACKET_SHA256_BY_SUITE,
    PACKET_SHA256,
    SOURCE_EPISODES_SHA256_BY_SUITE,
    account_decisions,
    build_expectation_report,
    build_phase1_account_ledger_projection,
    build_phase1_expectation,
    connector_sync_outcome,
    merge_phase2_view,
    read_packet,
    resolve_delivered_phase,
    sha256_json,
    version_token,
)

PACKET = Path("reports/v5/conformance/inputs/v4/v5_dev/selected-episodes.jsonl")
CARD = Path("reports/v5/conformance/inputs/v4/v5_dev/rendered-card.txt")
SOURCE_EPISODES_SHA256 = "6a3ffd0c173521cc6771418f92eac3df3da26bdc8e5bef676ac7000d280ce6a2"


def test_packet_hash_and_common_envelope_shape() -> None:
    episodes, digest = read_packet(PACKET)
    assert digest == PACKET_SHA256
    assert hashlib.sha256(CARD.read_bytes()).hexdigest() == CARD_SHA256
    assert len(episodes) == 12
    card_text = CARD.read_text(encoding="utf-8")
    report = build_expectation_report(
        episodes,
        SOURCE_EPISODES_SHA256,
        rendered_card_text=card_text,
    )
    assert report["schema"] == "identitybench.v5.reference-expectations.v1"
    assert report["proof_scope"] == "agent_view_only"
    assert len(report["phase_expectations"]) == 24
    assert all("required_target_multiset" in row for row in report["phase_expectations"])
    assert all(row["selection_provenance"] for row in report["phase_expectations"])
    assert all(
        len(row["selection_provenance"]) == (1 if row["phase_index"] == 1 else 2)
        for row in report["phase_expectations"]
    )
    assert {row["expected_first_failing_stage"] for row in report["diagnostic_witnesses"]} >= {
        1,
        2,
        3,
        4,
        5,
    }
    assert len({row["expected_violation_code"] for row in report["diagnostic_witnesses"]}) == 43
    assert report["underdefined"] == []
    assert report["episodes_sha256"] == SOURCE_EPISODES_SHA256
    assert len(report["policy_card_anchor_map"]) == 39
    assert any(
        row["expected_first_failing_stage"] == 5
        and row["expected_violation_code"] == "HR_FEED_AUTHORITY_MISMATCH"
        for row in report["diagnostic_witnesses"]
    )
    assert {
        (row["expected_overgrant"], row["expected_availability_breach"])
        for row in report["safety_witnesses"]
    } == {(False, False), (True, False), (False, True), (True, True)}
    assert len({row["target"]["mutation_kind"] for row in report["safety_witnesses"]}) == 12
    assert len(report["snapshot_witnesses"]) >= 4
    assert all(
        set(row["expected_metrics"])
        == {
            "G-SRR",
            "Sem-SRR@commit",
            "Action-SRR@last_stage",
            "G-PROP",
            "G-PROP_reset",
            "p1_staged",
            "p2_observed",
            "both_phases_staged",
        }
        for row in report["snapshot_witnesses"]
    )
    snapshots = {row["witness_id"]: row["expected_metrics"] for row in report["snapshot_witnesses"]}
    assert all(value == 1 for value in snapshots["snapshot:all-committed-pass"].values())
    assert all(value == 0 for value in snapshots["snapshot:no-observed-stages"].values())
    assert snapshots["snapshot-p1-only"]["p1_staged"] == 1
    assert snapshots["snapshot-p1-only"]["p2_observed"] == 0
    assert snapshots["snapshot-staged-but-p2-commit-blocked"]["Action-SRR@last_stage"] == 1
    assert snapshots["snapshot-staged-but-p2-commit-blocked"]["G-SRR"] == 0
    for phase in report["phase_expectations"]:
        if phase["phase_index"] != 1:
            continue
        optimizations = [
            mutation
            for mutation in phase["mutations"]
            if mutation["mutation_kind"] == "OPTIMIZATION_MUTATION"
        ]
        assert all(
            set(mutation["semantic_fields"]) == {"selected_ids", "objective_value"}
            and mutation.get("if_match") is not None
            and mutation.get("cite") is not None
            for mutation in optimizations
        )


def test_all_v4_suite_artifacts_bind_authorized_packets_and_sources() -> None:
    root = Path("reports/v5/conformance")
    for suite_id in ("v5_dev", "v5_lite", "v5_core", "v5_full"):
        input_root = root / "inputs" / "v4" / suite_id
        packet = input_root / "selected-episodes.jsonl"
        card = input_root / "rendered-card.txt"
        episodes, packet_digest = read_packet(packet)
        assert packet_digest == DELIVERED_PACKET_SHA256_BY_SUITE[suite_id]
        assert hashlib.sha256(card.read_bytes()).hexdigest() == CARD_SHA256

        report_path = root / suite_id / "agent_view_reference.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["suite_id"] == suite_id
        assert report["episodes_sha256"] == SOURCE_EPISODES_SHA256_BY_SUITE[suite_id]
        assert len(report["phase_expectations"]) == 2 * len(episodes)
        assert report["underdefined"] == []
        assert len(report["policy_card_anchor_map"]) == 39


def test_p1_builder_has_no_phase2_argument_or_data_dependency() -> None:
    episodes, _ = read_packet(PACKET)
    original = episodes[0]
    baseline_phase, baseline = build_phase1_expectation(
        original["episode_id"],
        original["phase1_manifest"],
        original["phase1_slices"],
        original["q1_slice_ids_p1"],
        original["q2_slice_ids_p1"],
        original["policy_card"],
    )

    changed_phase2 = copy.deepcopy(original)
    changed_phase2["phase2_base_manifest"]["prior_phase_ledger"] = {"poison": "not passed to P1"}
    changed_phase2["phase2_slices"] = {"unselected": {"tables": []}}
    phase2_only_changed, same_expectation = build_phase1_expectation(
        changed_phase2["episode_id"],
        changed_phase2["phase1_manifest"],
        changed_phase2["phase1_slices"],
        changed_phase2["q1_slice_ids_p1"],
        changed_phase2["q2_slice_ids_p1"],
        changed_phase2["policy_card"],
    )
    assert baseline == same_expectation
    assert baseline_phase.selected_slice_ids == phase2_only_changed.selected_slice_ids


def test_v4_itsm_visitation_drives_preconditions_and_citations() -> None:
    episodes, _ = read_packet(PACKET)
    episode = episodes[0]
    phase, expectation = build_phase1_expectation(
        episode["episode_id"],
        episode["phase1_manifest"],
        episode["phase1_slices"],
        episode["q1_slice_ids_p1"],
        episode["q2_slice_ids_p1"],
        episode["policy_card"],
    )
    decisions = account_decisions(phase)
    mutation_by_target = {
        row["target_entity_id"]: row
        for row in expectation["mutations"]
        if row["mutation_kind"] == "CONNECTOR_SYNC_MUTATION"
    }
    checked = 0

    def cite(rule_id: str, row: dict[str, object] | None) -> str | None:
        evidence_id = row.get("evidence_id") if row else None
        return f"{rule_id}:{evidence_id}" if evidence_id else None

    for connector_id in phase.manifest["work_queue"]["target_connector_ids"]:
        result = connector_sync_outcome(phase, connector_id, decisions)
        if not result["itsm_ticket_actions"]:
            continue
        schema = result["schema"]
        candidates = {
            account_id
            for account_id in (
                schema.get("itsm_primary_fulfiller_account_id"),
                schema.get("itsm_backup_fulfiller_account_id"),
                *(
                    ticket.get("assigned_to_account_id")
                    for ticket in schema.get("open_itsm_tickets", ())
                ),
            )
            if account_id is not None
        }
        assert result["evaluated_fulfiller_account_ids"] == sorted(candidates)

        precondition_ids = {connector_id, *candidates}
        expected_cites = {cite("CONN-V5-01", schema)}
        expected_cites.update(cite("CONN-V5-01", page) for page in result["used_pages"])
        expected_cites.update(
            cite("CONN-V5-01", phase.table_rows.get("idp_accounts", {}).get(token.split(":", 1)[0]))
            for token in result["drift_tokens"]
        )
        expected_cites.add(cite("ITSM-V5-01", schema))
        expected_cites.update(
            cite("ITSM-V5-01", ticket) for ticket in result["matched_itsm_tickets"]
        )
        for account_id in result["evaluated_fulfiller_account_ids"]:
            account = phase.table_rows.get("idp_accounts", {}).get(account_id)
            decision = decisions.get(account_id, {})
            person_id = decision.get("bound_person_id")
            if account is not None:
                expected_cites.add(cite("ITSM-V5-01", account))
                precondition_ids.add(account_id)
            if person_id is not None:
                person = phase.table_rows.get("hr_persons", {}).get(person_id)
                expected_cites.add(cite("ITSM-V5-01", person))
                precondition_ids.add(person_id)

        expected_if_match = sorted(
            {
                token
                for entity_id in precondition_ids
                if (token := version_token(phase, entity_id)) is not None
            }
        )
        expected_cites.discard(None)
        mutation = mutation_by_target[connector_id]
        assert mutation["if_match"] == expected_if_match
        assert mutation["cite"] == sorted(expected_cites)
        checked += 1

    assert checked > 0


def test_phase2_carryover_applies_invalidation_and_keeps_selection_provenance() -> None:
    episodes, _ = read_packet(PACKET)
    episode = episodes[0]
    p1 = resolve_delivered_phase(
        episode["episode_id"],
        1,
        episode["phase1_manifest"],
        episode["phase1_slices"],
        episode["q1_slice_ids_p1"],
        episode["q2_slice_ids_p1"],
        episode["policy_card"],
    )
    p2_new = resolve_delivered_phase(
        episode["episode_id"],
        2,
        episode["phase2_base_manifest"],
        episode["phase2_slices"],
        episode["q1_slice_ids_p2"],
        episode["q2_slice_ids_p2"],
        episode["policy_card"],
    )
    p2 = merge_phase2_view(p1, p2_new)
    assert p2.selected_slice_ids == p2_new.selected_slice_ids
    invalidated = set(p2_new.manifest["invalidated_entity_ids"])
    for person_id in invalidated:
        refreshed = p2_new.table_rows.get("hr_persons", {}).get(person_id)
        if refreshed is None:
            assert person_id not in p2.table_rows.get("hr_persons", {})
        else:
            assert p2.table_rows["hr_persons"][person_id] == refreshed
    assert "acct_01" in p2.table_rows.get("idp_accounts", {})
    p2_expected = build_expectation_report(
        episodes[:1],
        SOURCE_EPISODES_SHA256,
        rendered_card_text=CARD.read_text(encoding="utf-8"),
    )["phase_expectations"][1]
    assert p2_expected["carried_selected_p1_slice_ids"] == list(p1.selected_slice_ids)
    assert [row["phase_index"] for row in p2_expected["selection_provenance"]] == [1, 2]
    assert all(
        digest["payload_sha256"] == sha256_json(p2.slices[digest["slice_id"]])
        for digest in p2.payload_hashes
    )


def test_invalidated_p1_people_are_removed_before_selected_p2_refresh() -> None:
    episodes, _ = read_packet(PACKET)

    def evidence_ids(value: object) -> set[str]:
        found: set[str] = set()
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "evidence_id" and isinstance(item, str):
                    found.add(item)
                else:
                    found.update(evidence_ids(item))
        elif isinstance(value, list):
            for item in value:
                found.update(evidence_ids(item))
        return found

    checked = 0
    for episode in episodes:
        p1 = resolve_delivered_phase(
            episode["episode_id"],
            1,
            episode["phase1_manifest"],
            episode["phase1_slices"],
            episode["q1_slice_ids_p1"],
            episode["q2_slice_ids_p1"],
            episode["policy_card"],
        )
        p2_new = resolve_delivered_phase(
            episode["episode_id"],
            2,
            episode["phase2_base_manifest"],
            episode["phase2_slices"],
            episode["q1_slice_ids_p2"],
            episode["q2_slice_ids_p2"],
            episode["policy_card"],
        )
        p2 = merge_phase2_view(p1, p2_new)
        p2_evidence = evidence_ids({table: list(rows) for table, rows in p2.tables.items()})
        for person_id in set(p2_new.manifest["invalidated_entity_ids"]):
            old_row = p1.table_rows.get("hr_persons", {}).get(person_id)
            refreshed_row = p2_new.table_rows.get("hr_persons", {}).get(person_id)
            resolved_row = p2.table_rows.get("hr_persons", {}).get(person_id)
            if old_row is None:
                continue
            if refreshed_row is not None:
                assert resolved_row == refreshed_row
                assert old_row != refreshed_row
                assert not (evidence_ids(old_row) - evidence_ids(refreshed_row)) & p2_evidence
                checked += 1
            else:
                assert resolved_row is None
    assert checked == 12


def test_p1_account_ledger_projection_uses_only_p1_inputs() -> None:
    episodes, _ = read_packet(PACKET)
    for episode in episodes:
        phase, expectation = build_phase1_expectation(
            episode["episode_id"],
            episode["phase1_manifest"],
            episode["phase1_slices"],
            episode["q1_slice_ids_p1"],
            episode["q2_slice_ids_p1"],
            episode["policy_card"],
        )
        projected = build_phase1_account_ledger_projection(phase, expectation["mutations"])
        visible = episode["phase2_base_manifest"]["prior_phase_ledger"]["account_outcomes"]
        assert projected == visible
