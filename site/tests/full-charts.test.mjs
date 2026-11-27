import assert from 'node:assert/strict';
import test from 'node:test';
import { installDOM } from './dom-stub.mjs';
import { createStore } from '../assets/scripts/main.js';
import { fullChartData, mountAnalysis } from '../assets/scripts/charts/full.js';
import { paretoFrontier } from '../assets/scripts/charts/pareto.js';

function entry(id, score, cost) {
  return { id, name: id, rank: 1, vendor: 'google', vendor_label: 'Google', resolved_slots: 12,
    reasoning_effort: 'high', transport: 'Example CLI',
    safety_adjusted_score_tenths: score, og_total: 36, ab_total: 12,
    analysis: { category_pass_rates: { policy: 0 }, regime_g_srr: { R1: 0.25 },
      macro_cat_acc: 0.5, estimated_cost_usd: cost, estimated_cost_pricing_sha256: 'a'.repeat(64) } };
}

function mount(entries) {
  const dom = installDOM();
  const root = dom.root();
  root.innerHTML = ['pareto', 'safety', 'marginals', 'categories'].map((name) => `<div id="chart-${name}"></div>`).join('');
  const data = { models: [{ id: 'historical' }], confirmatory: { scenarios: 96, entries },
    categories: [{ id: 'policy', short: 'Policy', title: 'Access policy' }], regimes: [{ id: 'R1', label: 'Zero-trust' }] };
  const store = createStore({ selected: null, query: 'other', vendors: new Set(['other']) });
  const opened = [];
  mountAnalysis(root, data, store, (id) => opened.push(id));
  return { root, store, opened, data };
}

test('cost frontier and plotted coordinates preserve negative, zero and positive full-suite scores', () => {
  const { data, root } = mount([entry('negative', -240, 1), entry('zero', 0, 3),
    entry('positive', 480, 10), entry('dominated', -120, 9)]);
  assert.deepEqual(paretoFrontier(fullChartData(data).models).map((m) => m.id), ['negative', 'zero', 'positive']);
  const chart = root.querySelector('#chart-pareto');
  const marks = chart.querySelectorAll('.chart-mark');
  const ys = marks.map((m) => Number(m.querySelector('circle').getAttribute('cy')));
  assert.ok(ys[0] > ys[1] && ys[1] > ys[2]);
  assert.match(marks[0].getAttribute('aria-label'), /-25.00%/);
  assert.ok(chart.querySelectorAll('.chart-tick').some((t) => t.textContent === '-25%'));
  const buttons = chart.querySelector('.chart-segmented').querySelectorAll('button');
  buttons[2].dispatchEvent({ type: 'click' });
  assert.equal(buttons[2].getAttribute('aria-pressed'), 'true');
  assert.ok(chart.querySelectorAll('.chart-mark').every((m) =>
    m.querySelector('circle').getAttribute('cy') === chart.querySelector('circle').getAttribute('cy')));
});

test('optional analysis never creates zero-valued costs or heatmap cells for unreported runs', () => {
  const only = { ...entry('no-analysis', -5, 50), analysis: undefined };
  const { root } = mount([only]);
  assert.equal(root.querySelectorAll('svg').length, 1);
  for (const id of ['pareto', 'categories', 'marginals']) {
    assert.match(root.querySelector(`#chart-${id}`).textContent, /No full-suite/);
  }
  const safety = root.querySelector('#chart-safety').querySelector('.chart-mark');
  assert.match(safety.getAttribute('aria-label'), /Errors \/ scenario: 0.50/);
  assert.match(safety.getAttribute('aria-label'), /36 \/ 12/);
  assert.equal(mount([]).root.querySelectorAll('svg').length, 0);
});

test('heatmap keyboard selection opens the full configuration and clears table filters', () => {
  const { root, store, opened } = mount([entry('priced', 400, 15), { ...entry('unpriced', 0, 0), analysis: undefined }]);
  for (const id of ['categories', 'marginals']) {
    const chart = root.querySelector(`#chart-${id}`);
    const rows = chart.querySelectorAll('.chart-model-row');
    assert.equal(rows.length, 1);
    assert.match(rows[0].getAttribute('aria-label'), id === 'categories' ? /Access policy: 0.0%/ : /Zero-trust: 25.0%/);
    rows[0].dispatchEvent({ type: 'keydown', key: 'Enter' });
    assert.equal(store.get().selected, 'priced');
    assert.equal(store.get().query, '');
    assert.equal(store.get().vendors.size, 0);
  }
  assert.deepEqual(opened, ['priced', 'priced']);
});

test('a zero-dollar configuration is named alongside a log-scaled cost plot', () => {
  const { root } = mount([entry('free-run', 100, 0), entry('paid-run', 200, 1)]);
  const chart = root.querySelector('#chart-pareto');
  assert.match(chart.textContent, /\$0 estimates for free-run/);
  assert.equal(chart.querySelectorAll('.chart-mark').length, 1);
});

test('chart filters match transport labels, effort and the full table configuration ID', () => {
  const configuration = { ...entry('opaque-config', 100, 1), model_id: 'gpt-config',
    transport: 'Codex CLI', transport_id: 'codex_cli', reasoning_effort: 'xhigh' };
  const { root, store } = mount([configuration]);
  const mark = root.querySelector('#chart-safety').querySelector('.chart-mark');
  for (const query of ['Codex CLI', 'codex_cli', 'xhigh', 'gpt-config', 'opaque-config']) {
    store.set({ query, vendors: new Set() });
    assert.equal(mark.getAttribute('opacity'), '1', query);
  }
  store.set({ query: 'unrelated' });
  assert.equal(mark.getAttribute('opacity'), '0.3');
});
