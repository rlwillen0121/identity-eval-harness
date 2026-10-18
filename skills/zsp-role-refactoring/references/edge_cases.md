# ZSP Role Refactoring Edge Cases (`ZSP-001`)

| Scenario | Required Action | Rule ID |
| :--- | :--- | :--- |
| Standing human admin entitlement on prod app | Convert to `CONVERT_TO_JIT_ELIGIBLE` (`jit_max_ttl_seconds <= 3600`) | `ZSP-001` |
| Bound production service/workload account | Preserve required capability in `retained_entitlement_ids` | `ZSP-002` |
| Role count or direct grant cap exceeded | Consolidate into birthright/requestable bundles within $(K_{\max}, D_{\max})$ | `ZSP-003` |
