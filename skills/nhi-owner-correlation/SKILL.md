---
skill_id: nhi-owner-correlation
name: NHI Owner Correlation & 5-D Ontology Classification Runbook
description: Resolves human and Non-Human Identity (NHI) ownership using strict source priority and 5-D ontology classification.
version: "4.0.0"
category_tags: ["classification"]
token_estimate: 540
requires_policy_pack_min_version: "3.0"
---

# NHI Owner Correlation & 5-D Classification Runbook (`RECON-001`)

## 1. Authoritative Source Priority Order (`RECON-001`)
Resolve account ownership strictly in this priority order:
1. `active_workload_manifest` (`confidence >= 9500 bps`)
2. `vault_rotator_principal`
3. `cmdb_service_owner`
4. `provisioning_ticket_creator`
5. `stale_group_tag` (never sufficient to bind a production workload owner; flag or disable orphan)

## 2. 5-D Classification Ontology
Classify every account across `(principal_kind, account_subkind, subject_binding, accountability, workload_binding)`. Never disable a live `bound` workload identity (`prod_workload_bound`).

## 3. Validator Input Schema (`run_skill_validator`)
- `owner_resolution_source: str`, `classification_action: str`, `confidence_threshold_bps: int` (`9500`), `checked_items: int` (`1`)
