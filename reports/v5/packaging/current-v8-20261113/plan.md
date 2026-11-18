# Current v8 local packaging inventory

**Status: HOLD.** This is a read-only packaging proposal for `feat/v5-harness` at `f790e751c26d9667db588b95f9ef65cebe0f24e1`. It is not an export, test result, staged change, or commit. Root separately reports the source-health/four-validator set RELEASED with source, tests, and report fixtures frozen; this worker did not rerun or claim those checks.

## Proposed path set

The current candidate contains **172 files / 169,328,766 bytes (161.48 MiB)**:

- **146 paths / 99,499,172 bytes** refresh the stale 136-path candidate, add its six accepted overlays, and include four later safety-domain source/test/audit additions.
- **13 paths / 16,899,663 bytes** preserve the released v8 public-input package. Its manifest metadata says `RELEASED` and `PASS`; this pass did not rerun release validation.
- **1 coordinator helper / 145,767 bytes** plus **7 fixed-v3 snapshot inputs / 52,727,408 bytes** support the new synthetic and actual-inventory tests.
- **5 pinned protocol documents / 56,756 bytes** are needed by normal coordinator operation. Their current SHA-256 values match the helper's pins.

Current dirty v5 scope: 95 paths (19 source, 63 test, 12 dataset, 1 project config). The refreshed 146-path core group contains them all.

The old 136-file / 98,161,523-byte inventory is stale: **30 prior file hashes differ** from the current workspace. `inventory.json` contains the refreshed path-by-path hashes and old/new drift records. The coordinator and new test hashes match the final values accepted by root.

## Test and report dependencies

The dependency manifest separates direct reads from package provenance. Current v5 suites need their four dataset seals, episodes, and oracles. Tests also read historical v4/v5-dev/v6/v7 packets, current v8 packet/card inputs, shared family metadata, selected v6 review/capture fixtures, reference JSON fixtures, two structural-wire fixtures, and the exact seven-file archived v3 inventory source set.

Historical reference outputs and v6 qualification records remain **test fixtures only**. Their inclusion does not make them current qualified results. The v8 manifest points to the v7 package and v5 dataset artifacts included in the proposed set. Unapproved SPEC clarification drafts are listed separately and excluded; `SPEC-v5.md` remains normative.

The new coordinator test's synthetic inputs are created under `tmp_path`. Its fixed inventory test reads one archived snapshot manifest plus six source artifacts, all individually listed. The broader shared-results directory is not included.

## Export gaps

The proposed paths are an overlay on exact base `f790e751c26d9667db588b95f9ef65cebe0f24e1`, not a standalone source archive. Any later export must restore that base, preserve repository-relative paths, run from the exported repository root, and use the base `uv.lock` and unchanged modules. The historical optional 43-file / 212,105,560-byte v1-v3 manifest graph was not refreshed and is not needed for this selected test closure; full manifest replay or corpus rebuild is not claimed.

No dataset seals, v8 release, coordinator plan/receipt flow, Python import provenance, or tests were verified by this inventory task. The pending-review v8 manifest sidecar, pending-review v3-cursor-literal package, historical proof checkpoints, broad shared-results archive, dataset snapshots, and input history are excluded and preserved. Eleven foreign untracked files under `reports/v41-skill-orch-v2/` remain untouched; this inventory makes no clean-tree claim.

## Next sequence

1. Root reviews this exact 172-path candidate and decides release timing.
2. After release, build the isolated overlay on the exact base HEAD and verify file paths/hashes and runtime import origins.
3. Run only the selected exported dependency tests first; label any green as scoped export proof, not full-suite or source-health proof.
4. Keep source-health, milestone review lanes, benchmark validation, publication, and clean-tree provenance as separate gates.

Selected worker route: `gpt-6-luna`, `xhigh`, `fast`; implement lane assigned by root for bounded inventory refresh. This task ran no tests and performed no export or staging.
