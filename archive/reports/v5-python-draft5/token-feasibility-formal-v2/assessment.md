# Token-feasibility candidate assessment

This design-only candidate combines the shared protocol with the 26 category rule sections. It has 35 unique body headings (26 rule sections, eight boundary sections, and TOOLS), no missing headings, and a 39-key anchor mapping with the required five aliases. The exact-body checker reports 39 authored-body mismatches; treat that as a structural diagnostic only, not a semantic conformance verdict.

## Measurement

Measured on all 12 supplied `v5_dev` selected-episode packets using `o200k_base`, full `PolicyCardV5` JSON serialization, and packet-provided Q1 then Q2 slice order for each phase. The candidate full serialized card is 11,846–11,848 tokens (mean 11,847), against the 3,200 normative card cap. It is 9,548 tokens above the 2,300-token planning target and 8,648 above the normative cap. The parent-verified original shipped-card comparison is 16,554–16,556 tokens, so this candidate is 28.44% smaller.

Logical input is 21,639–22,023 tokens (mean 21,770.5), against 9,600. The empty-markdown card still yields 10,175–10,559 logical tokens; omitting the card object entirely yields 9,793–10,175. Thus the supplied packet's manifests, selected slices, and system text alone exceed the logical cap in these samples. This is a packet-specific observed measurement, not an impossibility claim about other slice selections or episodes.

## Remaining semantic gaps

Coverage files explicitly mark this as partial. The shared fragment does not reproduce the exhaustive per-kind `OVERGRANT`/`AVAIL_BREACH` Boolean table, all nested Stage 2 validators, all PRECOND/CITE helper equations/tie-breaks, the complete non-account ETag preimages, or the exact cluster-bootstrap/sign-flip and full-suite tie-break formulas. The `G-SRR@first_stage` eligibility/denominator remains undefined in the inspected spec. The IGA fragment additionally records abbreviated record validation, binding-candidate set, provider action formulas, optimizer feasibility/objective details, JML event associations, UAR custodian membership, and historical-session evidence construction. See `coverage-protocol.json` and `coverage-iga.json`; no full meaning-preservation claim is made.

No source, test, dataset, or Git mutations and no live model calls were performed. `measurements.json` records per-episode counts, serialization method, packet hash, anchor diagnostics, and SHA-256 hashes for the candidate fragments and coverage artifacts. This assessment intentionally has no self-hash.
