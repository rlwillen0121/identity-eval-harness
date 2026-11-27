import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import {
  CATEGORIES,
  FAMILIES,
  REGIMES,
  buildV53SiteData,
  buildFullSiteData,
  serializeV53SiteData,
} from '../build.mjs';
import { hasVendorLogo } from '../assets/scripts/logos.js';

function makeEntry({ rank = null, status = 'ranked', modelId, effort = 'high', transport = 'claude_cli', gSrr, cost, reasons = [] }) {
  return {
    rank,
    status,
    entry: { model_id: modelId, reasoning_effort: effort, transport_id: transport },
    eligibility: { eligible: reasons.length === 0, reasons },
    scores: {
      safety_adjusted_score: gSrr === null ? null : Math.max(-3, gSrr - 1 / 480),
      safety_adjusted_score_tenths: gSrr === null ? 40 : Math.round(gSrr * 48) * 10 - 1,
      safety_adjusted_interval: gSrr === null ? null : { lo: Math.max(-3, gSrr - 0.1), hi: Math.min(1, gSrr + 0.1) },
      g_srr: gSrr,
      g_srr_interval: gSrr === null ? null : { lo: Math.max(0, gSrr - 0.1), hi: Math.min(1, gSrr + 0.1) },
      rank_interval: rank === null ? null : { lo: rank, hi: rank + 0.5 },
      provisional_g_srr_bounds: status === 'in_progress' ? [0.1, 0.9] : null,
      macro_cat_acc: 0.85,
      state_acc: 0.91,
      resolved_slots: gSrr === null ? 4 : Math.round(gSrr * 48),
      planned_slots: 48,
      observed_slots: status === 'in_progress' ? 12 : 48,
      repeats: 1,
      commit_rate: 1.0,
      committed_slots: 48,
      og_total: 1,
      ab_total: 0,
      violations_per_committed_slot: 1 / 48,
      committed_violation_share: 1 / 48,
      category_pass_rates: Object.fromEntries(CATEGORIES.map((c, idx) => [c.id, idx === 0 ? 0.9 : 0.75])),
      subscores: { nhi_custody_pass_rate: 0.8, agent_delegation_pass_rate: 0.75 },
      iiq_to_isc_track: { pass_rate: 0.85, applicable_slots: 48 },
    },
    marginals: status === 'in_progress'
      ? null
      : {
          family_g_srr: Object.fromEntries(FAMILIES.map((f, i) => [f.id, i % 2 === 0 ? 1.0 : 0.5])),
          regime_g_srr: Object.fromEntries(REGIMES.map((r) => [r.id, 0.75])),
        },
    failure_taxonomy: {
      by_category: { access_policy: { check_counts: [{ check_name: 'sod_bundle', count: 4 }], top_checks: ['sod_bundle'] } },
      termination_counts: { EPISODE_COMPLETED: 48 },
      suppressed_cells: 2,
    },
    diagnostics: { mean_turns: 16.5, duplicate_read_rate: 0.05, format_error_turns: 0, median_turn_latency_s: 4.2, p95_turn_latency_s: 9.8, generation_tokens_per_second: 85.0, median_slot_wall_time_s: 70.0 },
    efficiency: { median_turns_per_resolved_slot: 15.0 },
    usage: {
      uncached_input_tokens: 1_000_000,
      cache_read_tokens: 12_000_000,
      cache_write_tokens: 200_000,
      output_tokens: 250_000,
      reasoning_tokens: 100_000,
      estimated_cost_usd: cost,
      cost_per_resolved_slot_usd: cost / 36,
    },
    metadata: {
      model_label: `${modelId}-t0-${effort}`,
      sampling: 'transport_default',
      system_prompt_channel: 'native',
      pricing_sha256: 'a'.repeat(64),
      receipt_sha256: 'b'.repeat(64),
      withheld_bundle_sha256: 'c'.repeat(64),
    },
  };
}

test('buildV53SiteData renders valid empty state from committed SEAL.json when no leaderboard exists', () => {
  const data = buildV53SiteData({
    leaderboardPath: '/nonexistent/leaderboard.json',
  });
  assert.equal(data.schema, 2);
  assert.equal(data.suite.id, 'v53_core');
  assert.equal(data.suite.episodes, 48);
  assert.equal(data.suite.turn_budget, 20);
  assert.equal(data.categories.length, 8);
  assert.equal(data.models.length, 0);
  assert.ok(data.suite.c_k);
  assert.ok(data.suite.round);
});

test('public site data contains only the full leaderboard and its matching seal', () => {
  const data = buildFullSiteData();
  const report = JSON.parse(fs.readFileSync(new URL('../../reports/v53/confirmatory.json', import.meta.url)));
  assert.equal(data.suite.id, 'v53_full');
  assert.equal(data.suite.scenarios, 96);
  assert.equal(data.suite.seal_sha256, report.seal_sha256);
  assert.equal(data.updated, report.published_on);
  const published = data.confirmatory.entries.map(({ id, name, transport, vendor, vendor_label, ...row }) => {
    assert.equal(id, [row.model_id, row.reasoning_effort, row.transport_id].map(encodeURIComponent).join('::'));
    assert.ok(name && transport && vendor && vendor_label);
    return row;
  });
  assert.deepEqual(published, report.entries);
  assert.equal(Object.hasOwn(data, 'models'), false);
  assert.equal(Object.hasOwn(data, 'history'), false);

  const empty = buildFullSiteData({ confirmatoryPath: '/nonexistent/confirmatory.json' });
  assert.equal(empty.updated, null);
  assert.equal(empty.confirmatory, null);
  assert.equal(empty.suite.scenarios, 96);
});

test('site build rejects legacy full rows until the native leaderboard is reimported', (t) => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'ib-legacy-full-'));
  t.after(() => fs.rmSync(tmp, { recursive: true, force: true }));
  const report = JSON.parse(fs.readFileSync(new URL('../../reports/v53/confirmatory.json', import.meta.url)));
  for (const row of report.entries) {
    delete row.rank;
    delete row.probe_receipt_bound;
  }
  const confirmatoryPath = path.join(tmp, 'confirmatory.json');
  fs.writeFileSync(confirmatoryPath, JSON.stringify(report));
  assert.throws(() => buildFullSiteData({ confirmatoryPath }), /Reimport the native offline leaderboard with --confirmatory/);
});

test('buildV53SiteData preserves §9 order regardless of cost and separates unranked/pending', () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'ib-v53-build-'));
  try {
    const lbPath = path.join(tmp, 'leaderboard.json');
    const histPath = path.join(tmp, 'history.json');
    const expensiveFirst = makeEntry({ rank: 1, modelId: 'gpt-6.2-astra', transport: 'codex_cli', gSrr: 0.875, cost: 180.5 });
    const cheaperSecond = makeEntry({ rank: 2, modelId: 'claude-haiku-5-5', transport: 'claude_cli', gSrr: 0.75, cost: 14.2 });
    const disqualified = makeEntry({ rank: null, status: 'unranked', modelId: 'gpt-6-luna', transport: 'codex_cli', gSrr: 0.5, cost: 4.0, reasons: [{ item: 4, code: 'native_tool_activity', detail: 'mcp_tool_call' }] });
    const inProgress = makeEntry({ rank: null, status: 'in_progress', modelId: 'gemini-4-neon', transport: 'agy_cli', gSrr: null, cost: 9.5 });

    fs.writeFileSync(lbPath, JSON.stringify({
      schema_version: 'identitybench.v5.3.leaderboard.v1',
      spec_version: 'SPEC-v5.3',
      protocol_id: 'identitybench.v5.3.protocol.v1',
      suite_id: 'v53_core',
      suite_visibility: 'private',
      test_only: false,
      pricing_sha256: 'a'.repeat(64),
      entries: [expensiveFirst, cheaperSecond],
      unranked_entries: [disqualified, inProgress],
      reliability_panel: { supplied: true, entries: [{ rank: 1, entry: expensiveFirst.entry, scores: { pass_hat_3: 0.81, pass_at_3: 0.94 } }] },
      withheld_until_sunset: ['per-slot rows'],
    }));
    fs.writeFileSync(histPath, JSON.stringify([
      { date: '2026-11-24', models: ['gpt-6.2-astra::high::codex_cli', 'claude-haiku-5-5::high::claude_cli'] },
    ]));

    const data = buildV53SiteData({ leaderboardPath: lbPath, historyPath: histPath });
    assert.deepEqual(data.models.map((m) => m.model_id), ['gpt-6.2-astra', 'claude-haiku-5-5']);
    assert.equal(data.models[0].rank, 1);
    assert.equal(data.models[1].rank, 2);
    assert.equal(data.unranked.length, 1);
    assert.equal(data.pending.length, 1);
    assert.equal(data.reliability_panel.entries.length, 1);
    for (const m of data.models) {
      assert.equal(hasVendorLogo(m.vendor), true, `missing logo for ${m.vendor}`);
    }
    const serialized = serializeV53SiteData(data);
    assert.ok(serialized.endsWith('\n'));

    const missingWithheld = path.join(tmp, 'leaderboard-bad.json');
    fs.writeFileSync(missingWithheld, JSON.stringify({
      schema_version: 'identitybench.v5.3.leaderboard.v1',
      spec_version: 'SPEC-v5.3',
      protocol_id: 'identitybench.v5.3.protocol.v1',
      suite_id: 'v53_core',
      suite_visibility: 'private',
      test_only: false,
      entries: [],
      unranked_entries: [],
      withheld_until_sunset: [],
    }));
    assert.throws(() => buildV53SiteData({ leaderboardPath: missingWithheld, historyPath: histPath }), /withheld_until_sunset/);
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

test('distinct full configurations keep distinct model links when identifiers contain colons', (t) => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'ib-full-ids-'));
  t.after(() => fs.rmSync(tmp, { recursive: true, force: true }));
  const report = JSON.parse(fs.readFileSync(new URL('../../reports/v53/confirmatory.json', import.meta.url)));
  const row = report.entries[0];
  report.entries = [
    { ...row, model_id: 'model', reasoning_effort: 'high::fast' },
    { ...row, model_id: 'model::high', reasoning_effort: 'fast' },
  ];
  const confirmatoryPath = path.join(tmp, 'confirmatory.json');
  fs.writeFileSync(confirmatoryPath, JSON.stringify(report));
  const { entries } = buildFullSiteData({ confirmatoryPath }).confirmatory;
  assert.notEqual(entries[0].id, entries[1].id);
});
