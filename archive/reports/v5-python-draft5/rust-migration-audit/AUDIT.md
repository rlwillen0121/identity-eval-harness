# Draft5 core historical-run Rust offline audit

Audited 24 historical configurations (3456 recorded slots, 3456 planned slots) using the preserved native Rust release binary. These are historical Python-executed model runs, regraded offline by Rust; no live model calls were made.

Outcome: {'complete': 24}. Protocol completion: {'protocol_complete': 18, 'provider_incomplete': 6}. Corrected: 0. Blocked: 0.

Semantic summary delta paths: 0; stored episode-score delta paths: 0. Native summary JSON is byte-identical for 24/24 configurations. Native report Markdown is byte-identical to the existing Python report for 24/24 configurations.

Provider failures: 580 slots. Parse failures: 40 slots. These remain distinct from turn-limit terminations and completion. Execution-valid rate is reported per configuration below; provider-incomplete runs are not publication-complete results.

| Configuration | Run completion | Execution-valid rate | Provider failures | Parse failures | Turn limit | Audit |
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

Binary SHA-256: `fb4c2099ae4e2659aa9d6e01f0f843ce199c44558aedab9d45316a98c97b978e`.

The native `v5 report` path loads sealed episodes and oracles, validates raw execution context, recomputes episode scoring inputs and scores from raw trajectories, and rejects disagreement with either attached scoring inputs or attached scores. All 24 commands succeeded. Comparisons then covered the complete parsed summary JSON, every stored episode score, and report Markdown bytes. The audit did not merely copy or aggregate stored scores.

All original run files and Python reports were unchanged after report generation: True. The inventory binds every original input file, manifest, episode log, pricing artifact, dataset seal, dataset episodes/oracles, existing Python report, and new native report by SHA-256. Each run dataset matches the canonical `datasets/v5_draft5/v5_core` artifacts; all manifest-to-file bindings pass.

The intentional artifact difference is placement in a separate audit output directory. Parsed summaries, semantic scores, Markdown, and historical manifest/episode provenance are preserved. Rust reports continue to bind the original run manifest and trajectories. The Rust executable hash belongs to the separate audit inventory, so no historical execution provenance was rewritten.

This is offline migration parity evidence on the existing core trajectories. It is not held-out tuning, a new live evaluation, dataset freeze sign-off, independent normative conformance, or publication approval. The preserved executable predates the concurrent integration; its exact identity is the recorded binary hash, not a claim about a later source HEAD.

Excluded unmanifested directories without matching Python summaries: v5_core_codex_56_luna, v5_core_local_strata_qwen_flash_next. These are outside the 24 manifested configurations.

Machine-readable evidence: [inventory.json](inventory.json). Native report bundles: [native/](native/). Reproduction collector: [audit_offline.py](audit_offline.py).

Public inventory source locators resolve relative to the explicit `--source-root` supplied to the collector. Native report locators resolve relative to this audit directory. Binary identity is its name and SHA-256; the initial private snapshot path is excluded from publication. Local binary snapshots are not publication artifacts.
