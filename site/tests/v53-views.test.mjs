import assert from 'node:assert/strict';
import test from 'node:test';

import { CATEGORIES, FAMILIES, REGIMES, buildV53SiteData } from '../build.mjs';
import { duration, formatInterval, gSrrPct, pct, tokPerSec, usd } from '../assets/scripts/format.js';
import { createStore, fillConfirmatory, leaderboardLede } from '../assets/scripts/main.js';
import { mount as mountTable } from '../assets/scripts/table.js';
import { mount as mountDetail } from '../assets/scripts/detail.js';
import { mount as mountUpdates } from '../assets/scripts/updates.js';
import { mount as mountPareto } from '../assets/scripts/charts/pareto.js';
import { mount as mountCategories } from '../assets/scripts/charts/categories.js';
import { mount as mountMarginals } from '../assets/scripts/charts/marginals.js';
import { mount as mountSafety } from '../assets/scripts/charts/safety.js';
import { installDOM } from './dom-stub.mjs';

function sampleSiteData() {
  const empty = buildV53SiteData({ leaderboardPath: '/nonexistent/leaderboard.json', confirmatoryPath: '/nonexistent/confirmatory.json' });
  const model = {
    id: 'claude-haiku-5-5::high::claude_cli',
    rank: 1,
    status: 'ranked',
    name: 'Claude Haiku 5.5',
    model_id: 'claude-haiku-5-5',
    reasoning_effort: 'high',
    transport_id: 'claude_cli',
    transport_label: 'Claude CLI',
    vendor: 'anthropic',
    vendor_label: 'Anthropic',
    first_published: '2026-11-24',
    eligibility: { eligible: true, reasons: [] },
    scores: {
      safety_adjusted_score: 357 / 480,
      safety_adjusted_score_tenths: 357,
      safety_adjusted_interval: { lo: 0.6, hi: 0.86 },
      g_srr: 0.75,
      g_srr_interval: { lo: 0.61, hi: 0.87 },
      rank_interval: { lo: 1, hi: 1 },
      macro_cat_acc: 0.88,
      state_acc: 0.92,
      resolved_slots: 36,
      planned_slots: 48,
      observed_slots: 48,
      repeats: 1,
      commit_rate: 1.0,
      committed_slots: 48,
      og_total: 2,
      ab_total: 1,
      violations_per_committed_slot: 3 / 48,
      category_pass_rates: Object.fromEntries(CATEGORIES.map((c) => [c.id, 0.8])),
      subscores: { nhi_custody_pass_rate: 0.85, agent_delegation_pass_rate: 0.75 },
      iiq_to_isc_track: { pass_rate: 0.9, applicable_slots: 48 },
    },
    marginals: {
      family_g_srr: Object.fromEntries(FAMILIES.map((f) => [f.id, 0.75])),
      regime_g_srr: Object.fromEntries(REGIMES.map((r) => [r.id, 0.75])),
    },
    failure_taxonomy: {
      by_category: { access_policy: { check_counts: [{ check_name: 'sod_bundle', count: 5 }] } },
      termination_counts: { EPISODE_COMPLETED: 48 },
      suppressed_cells: 3,
    },
    diagnostics: {
      mean_turns: 14.2,
      duplicate_read_rate: 0.04,
      format_error_turns: 0,
      median_turn_latency_s: 3.1,
      p95_turn_latency_s: 7.5,
      generation_tokens_per_second: 92.4,
      median_slot_wall_time_s: 45.0,
    },
    efficiency: { median_turns_per_resolved_slot: 13.0 },
    usage: {
      uncached_input_tokens: 500_000,
      cache_read_tokens: 8_000_000,
      cache_write_tokens: 100_000,
      output_tokens: 120_000,
      reasoning_tokens: 40_000,
      estimated_cost_usd: 11.4,
      cost_per_resolved_slot_usd: 0.32,
    },
    metadata: {
      sampling: 'transport_default',
      system_prompt_channel: 'native',
      pricing_sha256: 'a'.repeat(64),
      receipt_sha256: 'b'.repeat(64),
      withheld_bundle_sha256: 'c'.repeat(64),
    },
  };
  return {
    ...empty,
    vendors: [{ id: 'anthropic', label: 'Anthropic', count: 1 }],
    models: [model],
    history: [{ date: '2026-11-24', models: [model.id] }],
  };
}

test('formatting helpers format v5.3 metrics cleanly', () => {
  assert.equal(gSrrPct(0), '0%');
  assert.equal(pct(0.75, 1), '75.0%');
  assert.equal(usd(11.4), '$11.40');
  assert.equal(duration(45), '45s');
  assert.equal(duration(228), '3.8 min');
  assert.equal(duration(1320), '22 min');
  assert.equal(tokPerSec(92.4), '92.4 tok/s');
  assert.equal(formatInterval({ lo: 0.6, hi: 0.9 }, true), '60.0%–90.0%');
  assert.equal(formatInterval({ lo: 1, hi: 2 }, false), '#1.0–#2.0');
});

test('v5.3 views and charts mount cleanly in empty and populated states', () => {
  const dom = installDOM();
  const empty = buildV53SiteData({ leaderboardPath: '/nonexistent/leaderboard.json', confirmatoryPath: '/nonexistent/confirmatory.json' });
  const emptyStore = createStore({ sort: { key: 'rank', dir: 'asc' }, vendors: new Set(), query: '', selected: null, hover: null });
  assert.match(leaderboardLede(empty), /Awaiting/);

  const emptyTable = dom.root();
  mountTable(emptyTable, empty, emptyStore);
  assert.match(emptyTable.textContent, /No ranked runs are published yet/);

  for (const mountChart of [mountPareto, mountCategories, mountMarginals, mountSafety]) {
    const chartRoot = dom.root();
    const destroy = mountChart(chartRoot, empty, emptyStore);
    assert.equal(chartRoot.querySelectorAll('svg').length, 0);
    destroy();
  }

  const data = sampleSiteData();
  const store = createStore({ sort: { key: 'rank', dir: 'asc' }, vendors: new Set(), query: '', selected: data.models[0].id, hover: null });
  const tableRoot = dom.root();
  const cleanTable = mountTable(tableRoot, data, store);
  assert.match(tableRoot.textContent, /Claude Haiku 5\.5/);
  assert.match(tableRoot.textContent, /74\.4%/);
  assert.match(tableRoot.textContent, /36\/48/);
  assert.match(tableRoot.textContent, /By domain/);
  assert.match(tableRoot.textContent, /Safest first/);
  assert.match(tableRoot.textContent, /Fastest/);
  assert.match(tableRoot.textContent, /3 errors/);
  assert.match(tableRoot.textContent, /2 excess, 1 broken/);
  assert.match(tableRoot.textContent, /Time \/ task/);
  assert.equal(tableRoot.querySelectorAll('.category-track').length, CATEGORIES.length);
  assert.equal(tableRoot.querySelectorAll('.view-button').length, 4);
  const body = tableRoot.querySelector('tbody');
  const modelButton = body.querySelector('.model-name');
  modelButton.focus();
  body.dispatchEvent({ type: 'click', target: modelButton });
  assert.equal(store.get().selected, null);
  assert.equal(document.activeElement, body.querySelector('.model-name'));
  body.dispatchEvent({ type: 'click', target: document.activeElement });
  assert.equal(store.get().selected, data.models[0].id);
  assert.equal(document.activeElement, body.querySelector('.model-name'));

  const detailRoot = dom.root();
  const cleanDetail = mountDetail(detailRoot, data, store);
  assert.match(detailRoot.textContent, /sod_bundle/);
  assert.match(detailRoot.textContent, /Rare failures/);
  assert.match(detailRoot.textContent, /Technical details and audit hashes/);

  const updatesRoot = dom.root();
  const cleanUpdates = mountUpdates(updatesRoot, data, store);
  assert.match(updatesRoot.textContent, /24 Nov 2026/);

  for (const mountChart of [mountPareto, mountCategories, mountMarginals, mountSafety]) {
    const chartRoot = dom.root();
    const destroy = mountChart(chartRoot, data, store);
    assert.equal(chartRoot.querySelectorAll('svg').length, 1);
    destroy();
  }

  cleanTable();
  cleanDetail();
  cleanUpdates();
});

test('exact core ties share the same label in the table, tooltip, and Updates across dates', () => {
  const dom = installDOM();
  const data = sampleSiteData();
  data.models = [1, 2, 3].map((rank) => ({ ...data.models[0], id: `m${rank}`, rank }));
  data.history = [
    { date: '2026-11-26', models: ['m2', 'm3'] }, { date: '2026-11-24', models: ['m1'] },
  ];
  const store = createStore({ sort: { key: 'rank', dir: 'asc' }, vendors: new Set(), query: '' });
  const table = dom.root();
  mountTable(table, data, store);
  const cells = table.querySelectorAll('.rank-cell');
  assert.equal(cells.length, 3);
  for (const cell of cells) {
    assert.equal(cell.textContent, 'T1');
    assert.match(cell.getAttribute('title'), /^Rank T1 /);
  }
  const updates = dom.root();
  mountUpdates(updates, data, store);
  assert.equal(updates.textContent.match(/Core T1/g).length, 3);
});

test('full-suite results and publication dates render independently of core rankings', () => {
  const dom = installDOM();
  const data = sampleSiteData();
  const [model] = data.models;
  const report = {
    scenarios: 96,
    published_on: '2026-11-26',
    entries: [{
      rank: 1, probe_receipt_bound: false,
      name: model.name,
      model_id: model.model_id,
      reasoning_effort: model.reasoning_effort,
      transport_id: model.transport_id,
      transport: model.transport_label,
      safety_adjusted_score_tenths: -12,
      resolved_slots: 6,
      og_total: 3,
      ab_total: 2,
      step_limit_slots: 6,
      parse_failure_slots: 1,
    }],
  };
  const root = dom.root();
  root.innerHTML = `
    <p id="full-suite-summary"></p>
    <div class="full-suite-results"><table><tbody id="full-suite-rows"></tbody></table></div>
    <p id="full-suite-safety"></p><p id="full-suite-pending"></p>
  `;
  fillConfirmatory(root, null);
  assert.match(root.querySelector('#full-suite-summary').textContent, /No completed/);
  assert.equal(root.querySelector('.full-suite-results').hidden, true);

  const pending = { ...model, id: 'unmatched::high::claude_cli', name: 'Pending model' };
  fillConfirmatory(root, report, [model, pending]);
  assert.equal(root.querySelector('.full-suite-results').hidden, false);
  assert.match(root.querySelector('#full-suite-summary').textContent, /96 fresh private scenarios/);
  const row = root.querySelector('#full-suite-rows').querySelector('tr');
  assert.deepEqual([...row.querySelectorAll('td')].slice(0, 3).map((cell) => cell.textContent), ['1', '-1.25%', '6/96']);
  assert.match(row.textContent, /No bound probe receipt/);
  assert.match(row.textContent, /6 turn limits, 1 parse failure/);
  assert.equal(root.querySelector('#full-suite-pending').textContent, 'Awaiting full-suite results for Pending model.');

  const tie = { ...report.entries[0], model_id: 'second', probe_receipt_bound: true };
  fillConfirmatory(root, { ...report, entries: [report.entries[0], tie] });
  assert.deepEqual([...root.querySelectorAll('tr')].map((r) => r.querySelector('td').textContent), ['T1', 'T1']);
  assert.match(root.querySelector('#full-suite-summary').textContent, /1 of 2 runs have a bound context-probe receipt/);
  const updates = dom.root();
  mountUpdates(updates, { ...data, models: [], confirmatory: report }, createStore({}));
  assert.match(updates.textContent, /26 Nov 2026/);
  assert.match(updates.textContent, /96-scenario full suite/);
  assert.equal(updates.querySelector('.update-full-link').getAttribute('href'), '#full-suite');
});
