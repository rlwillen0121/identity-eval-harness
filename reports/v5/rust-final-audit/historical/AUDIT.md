# Draft5 core historical-run Rust offline audit

Audited 24 historical configurations (3456 recorded slots, 3456 planned slots) using the native Rust release binary. These are historical Python-executed model runs, regraded offline by Rust; no live model calls were made.

Audit outcomes: {'complete': 24}. Protocol completion: {'protocol_complete': 18, 'provider_incomplete': 6}. Corrected: 0.

Semantic summary delta paths: 0; stored episode-score delta paths: 0. Native summary JSON is byte-identical for 24/24 configurations. Native Markdown is byte-identical for 24/24 configurations.

Provider failures: 580 slots. Parse failures: 40 slots. These remain distinct from turn-limit terminations. Provider-incomplete runs are not publication-complete results.

| Configuration | Completion | Execution-valid rate | Provider failures | Parse failures | Turn limit | Audit |
|---|---|---:|---:|---:|---:|---|
| v5_core_agy_argon | protocol_complete | 1.0 | 0 | 0 | 0 | complete |
| v5_core_agy_carbon | protocol_complete | 1.0 | 0 | 0 | 0 | complete |
| v5_core_agy_flash | protocol_complete | 0.9861111111111112 | 0 | 2 | 5 | complete |
| v5_core_agy_gpt_oss_120b | protocol_complete | 0.8402777777777778 | 0 | 23 | 121 | complete |
| v5_core_agy_hydrogen | protocol_complete | 1.0 | 0 | 0 | 0 | complete |
| v5_core_cerebras_qwen38_27b | provider_incomplete | 0.09027777777777778 | 131 | 0 | 0 | complete |
| v5_core_claude_fable_5_2 | provider_incomplete | 0.7013888888888888 | 43 | 0 | 0 | complete |
| v5_core_claude_haiku | protocol_complete | 1.0 | 0 | 0 | 15 | complete |
| v5_core_claude_opus | protocol_complete | 1.0 | 0 | 0 | 0 | complete |
| v5_core_claude_opus_5 | provider_incomplete | 0.020833333333333332 | 141 | 0 | 0 | complete |
| v5_core_claude_sonnet | protocol_complete | 1.0 | 0 | 0 | 0 | complete |
| v5_core_codex_56_sol | protocol_complete | 1.0 | 0 | 0 | 4 | complete |
| v5_core_codex_56_terra | protocol_complete | 1.0 | 0 | 0 | 14 | complete |
| v5_core_codex_astra | protocol_complete | 1.0 | 0 | 0 | 0 | complete |
| v5_core_codex_daybreak | protocol_complete | 1.0 | 0 | 0 | 0 | complete |
| v5_core_codex_gpt6_sol | protocol_complete | 1.0 | 0 | 0 | 0 | complete |
| v5_core_codex_luna | protocol_complete | 1.0 | 0 | 0 | 124 | complete |
| v5_core_codex_sol | protocol_complete | 1.0 | 0 | 0 | 0 | complete |
| v5_core_codex_terra | protocol_complete | 1.0 | 0 | 0 | 0 | complete |
| v5_core_grok_4_6 | provider_incomplete | 0.020833333333333332 | 140 | 1 | 1 | complete |
| v5_core_grok_4_7 | protocol_complete | 0.9513888888888888 | 0 | 7 | 0 | complete |
| v5_core_local_qwen38_27b | protocol_complete | 0.9930555555555556 | 0 | 1 | 1 | complete |
| v5_core_muse_spark | provider_incomplete | 0.9513888888888888 | 1 | 6 | 0 | complete |
| v5_core_muse_wonder | provider_incomplete | 0.1388888888888889 | 124 | 0 | 0 | complete |

## Proof and scope

Binary SHA-256: `6414624e392c64afd1f714b5000bda66564584c2eda507fac17bb71b9a91583a`.

The native `v5 report` path loads sealed episodes and oracles, validates raw execution context, recomputes scoring inputs and scores from raw trajectories, and rejects disagreement with attached scoring inputs or scores. The inventory records command status and raw-regrading verification per configuration. Comparisons cover the complete parsed summary, stored scores, and report bytes.

All per-run input immutability and dataset binding checks passed: True. The inventory binds original run files, Python reports, canonical sealed dataset, and native outputs by SHA-256.

Final all-originals hash readback: True. Binary snapshot unchanged: True.

Inventory source_path and python_report_path resolve relative to explicit --source-root. Native report and local-only binary snapshot locators resolve relative to --out. Binary identity is its name and SHA-256; snapshots are excluded from publication. The output directory is separate. Historical execution provenance remains bound to the original manifest and trajectories; the Rust executable hash identifies this offline audit. The inventory records any semantic and artifact differences.

This is offline migration parity evidence, not held-out tuning, a live evaluation, dataset freeze sign-off, independent normative conformance, or publication approval. The binary hash makes no source-HEAD provenance claim.

Excluded unmanifested directories: v5_core_codex_56_luna, v5_core_local_strata_qwen_flash_next.

Machine-readable evidence: [inventory.json](inventory.json). Native report bundles: [native/](native/).

## Execution-history boundary

Completeness here describes the surviving fixed-slot artifacts. Offline regrading cannot reconstruct attempts or physical requests discarded by an earlier runner, or establish that historical resume behavior never replaced a slot. These remain historical Python-executed results, with their original execution provenance. The native executable was used only to regrade recorded trajectories. Fresh Rust evaluations require separate run directories and explicit live-run authorization.
