import assert from 'node:assert/strict';
import test from 'node:test';

import { CATEGORIES, FAMILIES, REGIMES, buildFullSiteData, buildV53SiteData } from '../build.mjs';
import { duration, formatInterval, gSrrPct, pct, tokPerSec, usd } from '../assets/scripts/format.js';
import { createStore, fillConfirmatory } from '../assets/scripts/main.js';
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

  for (const mountChart of [mountPareto, mountCategories, mountMarginals, mountSafety]) {
    const chartRoot = dom.root();
    const destroy = mountChart(chartRoot, data, store);
    assert.equal(chartRoot.querySelectorAll('svg').length, 1);
    destroy();
  }

  cleanTable();
  cleanDetail();
});

test('historical core ties share the same label in the table and tooltip', () => {
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
});

test('full-suite results and publication dates render independently of core rankings', () => {
  const dom = installDOM();
  const data = sampleSiteData();
  const [model] = data.models;
  const report = {
    scenarios: 96,
    published_on: '2026-11-26',
    entries: [{
      id: model.id, vendor: model.vendor, vendor_label: model.vendor_label,
      rank: 1, probe_receipt_bound: false,
      name: model.name,
      model_id: model.model_id,
      reasoning_effort: model.reasoning_effort,
      transport_id: model.transport_id,
      transport: model.transport_label,
      safety_adjusted_score_tenths: -12,
      resolved_slots: 6,
      analysis: { total_tokens: 1_250_000, mean_task_time_s: 120, estimated_cost_usd: 8, estimated_cost_pricing_sha256: 'a'.repeat(64) },
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
  `;
  fillConfirmatory(root, null);
  assert.match(root.querySelector('#full-suite-summary').textContent, /No completed/);
  assert.equal(root.querySelector('.full-suite-results').hidden, true);

  const store = fillConfirmatory(root, report);
  assert.equal(root.querySelector('.full-suite-results').hidden, false);
  assert.match(root.querySelector('#full-suite-summary').textContent, /96 private scenarios at simulated organizations/);
  const row = root.querySelector('.model-row');
  assert.equal(row.querySelector('.rank-cell').textContent, '1');
  assert.match(row.textContent, /−1.25%/);
  assert.doesNotMatch(row.textContent, /6\/96/);
  assert.equal(row.querySelector('.score-value').textContent, '−1.25%');
  const metrics = row.querySelectorAll('.usage-cell').map((cell) => cell.textContent);
  assert.deepEqual(metrics, ['1.3M', '$8.00', '2.0 min']);
  store.set({ selected: model.id });
  assert.match(root.querySelector('.expanded-metrics').textContent, /Total tokens1,250,000/);
  assert.match(root.textContent, /Clean solves6\/96/);
  assert.match(root.textContent, /No bound probe receipt/i);
  assert.match(root.textContent, /turn limit/i);
  assert.match(root.textContent, /parse failure/i);

  const other = {
    ...report.entries[0],
    id: 'second::high::codex_cli', name: 'Second', model_id: 'second', transport_id: 'codex_cli',
    vendor: 'openai', vendor_label: 'OpenAI', probe_receipt_bound: true, analysis: {},
  };
  const ties = fillConfirmatory(root, { ...report, entries: [report.entries[0], other] });
  const ranks = () => root.querySelectorAll('.rank-cell').map((cell) => cell.textContent);
  assert.deepEqual(ranks(), ['T1', 'T1']);
  for (const key of ['tokens', 'cost', 'time']) {
    for (const dir of ['asc', 'desc']) {
      ties.set({ sort: { key, dir } });
      assert.deepEqual(root.querySelectorAll('.model-row').map((r) => r.dataset.modelId), [model.id, other.id]);
    }
  }
  ties.set({ vendors: new Set(['openai']) });
  assert.deepEqual(ranks(), ['T1']);
  assert.match(root.querySelector('.model-row').textContent, /Second/);
  ties.set({ query: 'not a model' });
  assert.deepEqual(ranks(), []);
  const updates = dom.root();
  mountUpdates(updates, report);
  assert.match(updates.textContent, /26 Nov 2026/);
  assert.match(updates.textContent, /96-scenario leaderboard/);
  assert.equal(updates.querySelector('.update-full-link').getAttribute('href'), '#leaderboard');
});

test('safety and score controls reorder the full runs while preserving their published ranks', () => {
  const dom = installDOM();
  const { confirmatory } = buildFullSiteData();
  const root = dom.root();
  root.innerHTML = '<p id="full-suite-summary"></p><div class="full-suite-results"></div>';
  fillConfirmatory(root, confirmatory);

  const displayed = () => root.querySelectorAll('.model-row')
    .map((row) => [row.dataset.modelId, row.querySelector('.rank-cell').textContent]);
  const rankLabels = new Map(displayed());
  const idsAndRanks = (entries) => entries.map(({ id }) => [id, rankLabels.get(id)]);
  const safetyErrors = (row) => row.og_total + row.ab_total;

  root.querySelector('thead').dispatchEvent({ type: 'click', target: root.querySelector('[data-sort="violations"]') });
  const bySafety = [...confirmatory.entries].sort((a, b) => safetyErrors(a) - safetyErrors(b) || a.rank - b.rank);
  assert.deepEqual(displayed(), idsAndRanks(bySafety));

  const header = root.querySelector('thead');
  const score = root.querySelector('[data-sort="score"]');
  header.dispatchEvent({ type: 'click', target: score });
  assert.deepEqual(displayed(), idsAndRanks(confirmatory.entries));
  header.dispatchEvent({ type: 'click', target: score });
  const byAscendingScore = [...confirmatory.entries].sort((a, b) => a.safety_adjusted_score_tenths - b.safety_adjusted_score_tenths);
  assert.deepEqual(displayed(), idsAndRanks(byAscendingScore));
});

test('mobile sorting exposes the selected metric and its reversible column header', () => {
  const dom = installDOM();
  const { confirmatory } = buildFullSiteData();
  const root = dom.root();
  root.innerHTML = '<p id="full-suite-summary"></p><div class="full-suite-results"></div>';
  fillConfirmatory(root, confirmatory);
  const select = root.querySelector('.sort-select');
  const header = root.querySelector('thead');
  for (const sort of ['tokens', 'cost', 'time', 'violations', 'name', 'rank']) {
    select.value = sort;
    select.dispatchEvent({ type: 'change' });
    const metric = ['rank', 'name'].includes(sort) ? 'score' : sort;
    const visible = header.querySelector('.mobile-result');
    assert.equal(visible.dataset.key, metric);
    for (const row of root.querySelectorAll('.model-row')) {
      assert.equal(row.querySelectorAll('.mobile-result').length, 1);
      assert.equal(row.children.indexOf(row.querySelector('.mobile-result')), header.querySelectorAll('th').indexOf(visible));
    }
    if (sort === metric) {
      assert.equal(visible.getAttribute('aria-sort'), 'ascending');
      header.dispatchEvent({ type: 'click', target: visible.querySelector('button') });
      assert.equal(visible.getAttribute('aria-sort'), 'descending');
    }
  }
});
