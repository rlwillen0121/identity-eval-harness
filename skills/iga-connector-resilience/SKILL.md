---
skill_id: iga-connector-resilience
name: IGA Connector Pagination, ETag & Conditional Write Runbook
description: Enforces cursor pagination exhaustion, If-Match ETag conditional writes, and readback reconciliation on SCIM/REST/SQL connectors.
version: "4.0.0"
category_tags: ["connectors"]
token_estimate: 520
requires_policy_pack_min_version: "3.0"
---

# IGA Connector Resilience Runbook (`CONN-002-v3`)

## 1. Normative Connector Requirements
1. **Cursor Pagination Exhaustion:** Every connector sync loop MUST iterate `next_cursor` until exhausted (`next_cursor is None` or `has_more is False`). Never stop after page 1.
2. **Pre-Mutation Conditional Writes (`CONN-002-v3`):** Every target account or entitlement mutation MUST include `If-Match: <resource_etag>`. Unconditional `PUT` or `POST` overwrites are strictly prohibited.
3. **Missing Conditional Write Capability:** When a target endpoint lacks ETag / conditional write support and concurrent drift is detected, return `BLOCKED_UNSAFE_CONCURRENCY` rather than issuing an blind overwrite.
4. **Timeout / `UNKNOWN_OUTCOME` Readback:** On HTTP 504 or transport timeout, re-read the target account state and ETag before retrying.

## 2. Validator Input Schema (`run_skill_validator`)
- `unconditional_put: bool` (`False`), `missing_etag: bool` (`False`), `partial_pagination: bool` (`False`), `records_rejected: int` (`0`), `checked_items: int` (`1`)
