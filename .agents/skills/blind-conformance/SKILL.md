---
name: blind-conformance
description: >-
  Use when authoring or auditing blind conformance scenarios that compare the
  deterministic oracle with the normative specification and rendered episode
  views, without oracle leakage while deriving expected outcomes.
---

# Blind Conformance Skill (`lane-blind-conformance`)

Verify that each version's written rules and rendered policy views yield an unambiguous expected outcome, and identify divergences between those expectations and the implementation. Preserve a strict separation between deriving expected outcomes and inspecting implementation/gold data. Every assignment uses a fresh isolated subagent that has not seen forbidden implementation or gold data.

## The Isolation Rule

While deriving expected outcomes, do not read, search, or view `oracle.py`, `generator.py`, `verifier.py`, `baselines.py`, gold/reference solution traces, or `datasets/v5_*/oracles.jsonl`. Do not infer an expected result from implementation behavior.

- **v5 full-spec reference:** Derive from [`SPEC-v5.md`](../../../SPEC-v5.md) §§1–4 and its normative tables together with the approved [`SPEC-v5-ADDENDUM-01.md`](../../../SPEC-v5-ADDENDUM-01.md). This reference has zero access to oracle/generator/verifier/baseline implementation or gold traces and oracle datasets.
- **v5 agent-view-only reference:** Derive from `PolicyCardV5`, rendered episode prompts, and only the source slices delivered to the model. Do not rely on hidden fixture fields or metadata absent from that view.
- **Frozen versions:** Use the matching normative spec and rendered prompt/policy materials. Legacy v4.1 uses `SPEC-v4.1.md` and its rendered policy card; legacy v3 and v1 use their matching specs.

## Workflow

### Step 1: Derive Expected Outcomes Blindly

- For v5, cover all seven categories and relevant Stage 1–5 and Stage 5 classification behavior, including the 12 mutation kinds, `OVERGRANT` / `AVAIL_BREACH`, snapshots, and `RULE_TO_POLICY_CARD_ANCHORS_V5` as specified by `G-SPEC-01`. Apply the addendum only to its six non-account Phase-One ETag conventions and first-stage eligibility clarification.
- For v4.1 or older, use that version's own categories, paths, and gates. Never import v5 `agent_governance` expectations into legacy suites.
- Include boundary conditions, conflicting evidence, decoys, stale generations, fail-closed outcomes, and policy-visible distinctions.
- Keep full-spec expectations separate from agent-view-only expectations. The latter must use only the policy card and delivered slices.

### Step 2: Execute the Conformance Suite

After the expected outcomes are authored, run the matching suite to compare implementation behavior:

```bash
uv run pytest tests/conformance/v5 -v -m 'not live'
```

Use the matching legacy path for frozen versions, such as `tests/conformance/v41/`. For full repository verification, exclude live tests with `uv run pytest -q -m 'not live'`.

### Step 3: Triage Discrepancies

Do not change an expectation merely to match the implementation. Classify the failure in the matching version's `AMBIGUITIES.md` or report it to `orchestrate`:

1. **Oracle / generator bug:** The implementation violates the spec or rendered policy. Report the exact spec section for an `implement` repair.
2. **Spec / policy ambiguity:** The written or rendered rules permit multiple interpretations. For v5, record it in `tests/conformance/v5/AMBIGUITIES.md` or the review record; `SPEC-v5.md` is a fixed-hash input and must not be edited as part of this lane.

For v5, `G-SPEC-01` requires 100% agreement with both blind references. Any unresolved discrepancy blocks dataset freeze.
