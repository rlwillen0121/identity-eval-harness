---
skill_id: zsp-role-refactoring
name: Zero Standing Privilege (ZSP) & Role Refactoring Runbook
description: Refactors standing entitlements into ZSP eligible roles while preserving required workflow traces and role count ceilings.
version: "4.0.0"
category_tags: ["zsp_policy"]
token_estimate: 560
requires_policy_pack_min_version: "3.0"
---

# ZSP Role Refactoring Runbook (`ZSP-001`)

## 1. Semantic Authorization Envelope (`ZSP-001`)
1. **Workflow Trace Preservation:** Every refactored role policy MUST satisfy $W_{\text{required}} \subseteq R_{\text{reachable}} \subseteq W_{\text{allowed}}$.
2. **Standing-to-Eligible Conversion:** Privileged human admin entitlements MUST convert to `CONVERT_TO_JIT_ELIGIBLE` with `jit_max_ttl_seconds <= 3600` and `ZSP_ELIGIBLE_QUORUM`.
3. **Workload Continuity:** Bound production workload identities (`prod_workload_bound`) retain required standing runtime capabilities.

## 2. Validator Input Schema (`run_skill_validator`)
- `jit_max_ttl_seconds: int` (`3600`), `role_count_exceeded: bool` (`False`), `direct_grants_exceeded: bool` (`False`), `checked_items: int` (`1`)
