import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import {
  LEADERBOARD_SCHEMA,
  SPEC_VERSION,
  assertNoPrivateLeaks,
  importV52Leaderboard,
  importV52Run,
  validateV52Leaderboard,
  validateV52RunSummary,
} from '../../scripts/import-v52-run.mjs';

function sampleLeaderboard(overrides = {}) {
  return {
    schema_version: LEADERBOARD_SCHEMA,
    spec_version: SPEC_VERSION,
    protocol_id: 'identitybench.v5.2.protocol.v1',
    suite_id: 'v52_core',
    suite_visibility: 'private',
    ranked_suite: true,
    test_only: false,
    entries: [{ rank: 1, status: 'ranked', entry: { model_id: 'claude-haiku-5-5', reasoning_effort: 'high', transport_id: 'claude_cli' } }],
    unranked_entries: [],
    withheld_until_sunset: ['per-slot rows', 'transcripts'],
    ...overrides,
  };
}

function sampleSummary(overrides = {}) {
  return {
    spec_version: SPEC_VERSION,
    suite_id: 'v52_core',
    model_label: 'claude-haiku-5-5-t0-high',
    model_id: 'claude-haiku-5-5',
    provider_kind: 'claude_cli',
    effort: 'high',
    planned_slots: 48,
    observed_slots: 48,
    is_complete: true,
    ...overrides,
  };
}

test('validateV52Leaderboard rejects test_only unless explicitly allowed', () => {
  const board = sampleLeaderboard({ test_only: true });
  assert.throws(() => validateV52Leaderboard(board), /test_only/);
  assert.equal(validateV52Leaderboard(board, { allowTestOnly: true }).test_only, true);
});

test('validateV52Leaderboard rejects private per-slot rows and empty withheld_until_sunset', () => {
  assert.throws(
    () => validateV52Leaderboard(sampleLeaderboard({ withheld_until_sunset: [] })),
    /withheld_until_sunset/,
  );
  assert.throws(
    () => validateV52Leaderboard(sampleLeaderboard({ entries: [{ rank: 1, slots: [] }] })),
    /per-slot rows/,
  );
});

test('assertNoPrivateLeaks rejects local paths and private episode IDs', () => {
  assert.throws(() => assertNoPrivateLeaks('path /Users/alice/foo', 'v52_core', 'doc'), /local filesystem path/);
  assert.throws(() => assertNoPrivateLeaks('failed on v52_core_F01_R1_s00', 'v52_core', 'doc'), /private episode identifier/);
  assert.doesNotThrow(() => assertNoPrivateLeaks('public v52_dev_F01_R1_s00 ok', 'v52_dev', 'doc'));
});

test('importV52Leaderboard and importV52Run copy only publishable artifacts', () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'ib-v52-import-'));
  try {
    const lbSrc = path.join(tmp, 'lb_src');
    const lbOut = path.join(tmp, 'lb_out');
    fs.mkdirSync(lbSrc, { recursive: true });
    fs.writeFileSync(path.join(lbSrc, 'leaderboard.json'), JSON.stringify(sampleLeaderboard()));
    fs.writeFileSync(path.join(lbSrc, 'leaderboard.md'), '# Leaderboard\nClean report.\n');
    const lbRes = importV52Leaderboard({ srcDir: lbSrc, outDir: lbOut });
    assert.equal(lbRes.rankedCount, 1);
    assert.ok(fs.existsSync(path.join(lbOut, 'leaderboard.json')));
    assert.ok(fs.existsSync(path.join(lbOut, 'leaderboard.md')));

    const runSrc = path.join(tmp, 'run_src');
    const runOut = path.join(tmp, 'run_out');
    fs.mkdirSync(path.join(runSrc, 'configs'), { recursive: true });
    fs.writeFileSync(path.join(runSrc, 'summary.json'), JSON.stringify(sampleSummary()));
    fs.writeFileSync(path.join(runSrc, 'report.md'), '# Run Report\nClean aggregate.\n');
    fs.writeFileSync(path.join(runSrc, 'manifest.json'), '{"slot_order":["v52_core_F01_R1_s00"]}');
    fs.writeFileSync(path.join(runSrc, 'episodes.jsonl'), '{"episode_id":"v52_core_F01_R1_s00"}\n');
    fs.writeFileSync(path.join(runSrc, 'probe_receipt.json'), '{"detail":"/Users/local/sandbox"}');

    const runRes = importV52Run({ runDir: runSrc, name: 'haiku-55', outDir: runOut });
    assert.equal(runRes.observedSlots, 48);
    assert.ok(fs.existsSync(path.join(runOut, 'haiku-55', 'summary.json')));
    assert.ok(fs.existsSync(path.join(runOut, 'haiku-55', 'report.md')));
    assert.equal(fs.existsSync(path.join(runOut, 'haiku-55', 'manifest.json')), false);
    assert.equal(fs.existsSync(path.join(runOut, 'haiku-55', 'episodes.jsonl')), false);
    assert.equal(fs.existsSync(path.join(runOut, 'haiku-55', 'probe_receipt.json')), false);
    assert.equal(fs.existsSync(path.join(runOut, 'haiku-55', 'configs')), false);
    assert.equal(validateV52RunSummary(sampleSummary()).suite_id, 'v52_core');
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});
