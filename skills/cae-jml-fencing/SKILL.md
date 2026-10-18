---
skill_id: cae-jml-fencing
name: Continuous Access Evaluation (CAE) & JML Lifecycle Fencing Runbook
description: Enforces exact post-trigger CAE exposure bounds (<= E_max) and commit-time (person_lifecycle_gen, account_gen, engagement_id) fencing.
version: "4.0.0"
category_tags: ["jml_cae_drift"]
token_estimate: 550
requires_policy_pack_min_version: "3.0"
---

# CAE & JML Lifecycle Fencing Runbook (`CAE-001`)

## 1. Exact CAE Post-Trigger Exposure Inequality (`CAE-001`)
For every involuntary termination or credential compromise trigger, complete session revocation and credential rotation such that:
$$\max(0,\; t_{\text{issuance\_stop}} - t_{\text{reaction\_start}}) + \text{residual\_token\_bound} + \text{cache\_bound} + \text{clock\_skew\_bound} \le E_{\max}$$
where $E_{\max} \le 60\text{s}$ under standard policy.

## 2. Commit-Time Lifecycle Generation Fencing
Supply `resource_preconditions` with `(if_match_etag, person_lifecycle_gen, account_gen, engagement_id)` on human accounts and `(if_match_etag, account_gen)` on unowned NHIs.

## 3. Validator Input Schema (`run_skill_validator`)
- `exposure_seconds: int` (`0`), `e_max_seconds: int` (`60`), `missing_lifecycle_fence: bool` (`False`), `checked_items: int` (`1`)
