# NHI Owner Correlation Edge Cases (`RECON-001`)

| Scenario | Required Action | Rule ID |
| :--- | :--- | :--- |
| `active_workload_manifest` vs `stale_group_tag` conflict | Prefer `active_workload_manifest` and `BIND_OWNER` | `RECON-001` |
| Tied timestamp across primary sources on critical/SOX app | Escalate via `request_team_steer(trigger_reason=SOURCE_AUTHORITY_CONFLICT)` | `RECON-002` |
| Unattributed account with `workload_binding == unbound` | Select `DISABLE_ORPHAN` | `RECON-003` |
