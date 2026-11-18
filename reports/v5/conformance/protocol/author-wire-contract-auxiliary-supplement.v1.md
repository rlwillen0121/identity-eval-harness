# Auxiliary clarification for the shared-reference wire contract

This supplement applies to the existing
`identitybench.v5.shared-conformance.v1` protocol and
[author wire contract](author-wire-contract.v1.md), whose pinned SHA-256 is
`f4f022bb447f7fc51ba3e45ee8fe6a4c7ddc586c791f02f84db6660174f07cd8`. It
adds no protocol version, wire field, metric, or proof dimension.

For the existing `p1_first_stage_snapshot_ok` score metric, use the emitted
phase 1 `FIRST_STAGE` snapshot: report integer 1 when `stage1_ok` through
`stage5_ok` are all true, otherwise integer 0. If no phase 1 `FIRST_STAGE`
snapshot is emitted, report 0.

This is an auxiliary consistency summary, not a headline metric or separate
normative proof dimension. It does not resolve or select the eligibility rule
for `G-SRR@first_stage`.
