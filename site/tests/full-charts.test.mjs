import assert from 'node:assert/strict';
import test from 'node:test';
import { installDOM } from './dom-stub.mjs';
import { createStore } from '../assets/scripts/main.js';
import { fullChartData, mountAnalysis } from '../assets/scripts/charts/full.js';
import { paretoFrontier } from '../assets/scripts/charts/pareto.js';

function entry(id, score, cost, totalTokens = 12500, meanTaskTime = 30.5) {
  return { id, name: id, rank: 1, vendor: 'google', vendor_label: 'Google', resolved_slots: 12,
    reasoning_effort: 'high', transport: 'Example CLI',
    safety_adjusted_score_tenths: score, og_total: 36, ab_total: 12,
    analysis: { category_pass_rates: { policy: 0 }, regime_g_srr: { R1: 0.25 },
      macro_cat_acc: 0.5, estimated_cost_usd: cost, estimated_cost_pricing_sha256: 'a'.repeat(64),
      total_tokens: totalTokens, mean_task_time_s: meanTaskTime } };
}

function mount(entries, { width = 720 } = {}) {
  const dom = installDOM();
  const root = dom.root();
  root.innerHTML = ['pareto', 'safety', 'marginals', 'categories'].map((name) => `<div id="chart-${name}"></div>`).join('');
  root.rect = { width };
  const data = { models: [{ id: 'historical' }], confirmatory: { scenarios: 96, entries },
    categories: [{ id: 'policy', short: 'Policy', title: 'Access policy' }], regimes: [
      { id: 'R1', label: 'Zero-trust' }, { id: 'R2', label: 'Multi-affiliate' },
      { id: 'R3', label: 'Hybrid cloud' }, { id: 'R4', label: 'M&A' },
    ] };
  const store = createStore({ selected: null, query: 'other', vendors: new Set(['other']) });
  const opened = [];
  mountAnalysis(root, data, store, (id) => opened.push(id));
  return { root, store, opened, data, dom };
}

function metricButton(chart, id) {
  return chart.querySelector('.chart-efficiency-switch').querySelectorAll('button')
    .find((button) => button.getAttribute('data-metric') === id);
}

test('cost frontier and plotted coordinates preserve negative, zero and positive full-suite scores', () => {
  const { data, root } = mount([entry('negative', -240, 1, 12500, 30.5), entry('zero', 0, 3, 22500, 62),
    entry('positive', 480, 10, 85000, 123), entry('dominated', -120, 9, 76000, 118)]);
  assert.deepEqual(paretoFrontier(fullChartData(data).models).map((m) => m.id), ['negative', 'zero', 'positive']);
  assert.deepEqual(fullChartData(data).models[0].usage, {
    estimated_cost_usd: 1, total_tokens: 12500, mean_task_time_s: 30.5,
  });
  const chart = root.querySelector('#chart-pareto');
  const marks = chart.querySelectorAll('.chart-mark');
  const ys = marks.map((m) => Number(m.querySelector('circle').getAttribute('cy')));
  assert.ok(ys[0] > ys[1] && ys[1] > ys[2]);
  assert.match(marks[0].getAttribute('aria-label'), /-25.00%/);
  assert.ok(chart.querySelectorAll('.chart-tick').some((t) => t.textContent === '-25%'));
  const xTickLabels = () => chart.querySelectorAll('.chart-tick').filter((tick) => tick.getAttribute('text-anchor') === 'middle');
  assert.ok(xTickLabels().length >= 3 && xTickLabels().length <= 5);
  const buttons = chart.querySelector('.chart-efficiency-switch').querySelectorAll('button');
  assert.equal(chart.querySelector('.chart-efficiency-switch').getAttribute('aria-label'), 'Horizontal axis metric');
  assert.deepEqual(buttons.map((button) => button.getAttribute('aria-label')),
    ['Estimated total cost', 'Total tokens', 'Mean time per task']);
  const firstMark = chart.querySelector('[data-model-id="negative"]');
  const firstCircle = firstMark.querySelector('circle');
  const costX = firstCircle.getAttribute('cx');
  assert.match(firstMark.getAttribute('aria-label'), /Estimated total cost: \$1\.00/);
  assert.match(firstMark.getAttribute('aria-label'), /Total tokens: 12\.5K/);
  assert.match(firstMark.getAttribute('aria-label'), /Mean time per task: 30\.5 s/);
  firstMark.focus();
  const tip = chart.querySelector('.chart-tooltip');
  assert.equal(tip.hidden, false);
  assert.match(tip.textContent, /Total tokens/);
  assert.match(tip.textContent, /Mean time per task/);

  buttons[1].dispatchEvent({ type: 'click' });
  assert.equal(buttons[1].getAttribute('aria-pressed'), 'true');
  assert.ok(xTickLabels().length >= 3 && xTickLabels().length <= 5);
  assert.notEqual(chart.querySelector('[data-model-id="negative"] circle').getAttribute('cx'), costX);
  const tokenYs = chart.querySelectorAll('.chart-mark').map((m) => m.querySelector('circle').getAttribute('cy'));
  buttons[2].dispatchEvent({ type: 'click' });
  assert.equal(buttons[2].getAttribute('aria-pressed'), 'true');
  assert.ok(xTickLabels().length >= 3 && xTickLabels().length <= 5);
  assert.ok(xTickLabels().every((tick) => /(?:s|min|h)$/.test(tick.textContent)));
  assert.notEqual(chart.querySelector('[data-model-id="negative"] circle').getAttribute('cx'), costX);
  assert.deepEqual(chart.querySelectorAll('.chart-mark').map((m) => m.querySelector('circle').getAttribute('cy')), tokenYs);

  const scoreSelect = chart.querySelector('select[aria-label="Vertical axis score metric"]');
  scoreSelect.value = 'g_srr';
  scoreSelect.dispatchEvent({ type: 'change' });
  assert.match(chart.querySelector('[data-model-id="negative"]').getAttribute('aria-label'),
    /Clean solve rate \(96 scenarios\): 12\.5%/);
  scoreSelect.value = 'macro_cat_acc';
  scoreSelect.dispatchEvent({ type: 'change' });
  assert.match(chart.querySelector('[data-model-id="negative"]').getAttribute('aria-label'),
    /Domain accuracy averaged across 96 scenarios: 50\.0%/);
});

test('primary chart title and frontier caption match all nine score-resource combinations', () => {
  const { root } = mount([entry('first', 100, 1), entry('second', 300, 5)]);
  const chart = root.querySelector('#chart-pareto');
  const querySelectorAll = root.querySelectorAll.bind(root);
  root.querySelectorAll = (selector) => {
    const nodes = querySelectorAll(selector);
    if (selector !== '.chart-sub') return nodes;
    return { length: nodes.length, [Symbol.iterator]: function* iterator() { yield* nodes; } };
  };
  const scoreSelect = chart.querySelector('select[aria-label="Vertical axis score metric"]');
  const scores = [
    { id: 'safety_adjusted_score', title: 'Safety-adjusted score', frontier: 'score' },
    { id: 'g_srr', title: 'Clean solve rate', frontier: 'clean solve rate' },
    { id: 'macro_cat_acc', title: 'Domain accuracy', frontier: 'domain accuracy' },
  ];
  const resources = [
    { id: 'cost', title: 'estimated cost', frontier: 'estimated cost' },
    { id: 'tokens', title: 'total tokens', frontier: 'token total' },
    { id: 'time', title: 'time per task', frontier: 'time per task' },
  ];
  for (const score of scores) {
    scoreSelect.value = score.id;
    scoreSelect.dispatchEvent({ type: 'change' });
    for (const resource of resources) {
      metricButton(chart, resource.id).dispatchEvent({ type: 'click' });
      assert.equal(chart.querySelector('.chart-title').textContent, `${score.title} vs. ${resource.title}`);
      assert.equal([...chart.querySelectorAll('.chart-sub')].find((node) => node.parentNode === chart)?.textContent,
        `The line shows the best ${score.frontier} at or below each ${resource.frontier} among plotted runs.`);
      assert.match(chart.querySelector('svg').getAttribute('aria-label'), new RegExp(`${score.title.toLowerCase()} across 96 scenarios against`));
    }
  }
  assert.equal(root.querySelectorAll('.analysis-chart').length, 4);
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

test('zero and missing time remain distinct and zero-only metrics get an exclusion note', () => {
  const zero = entry('zero-time', 100, 1, 1000, 0);
  const missing = entry('unknown-time', 200, 2, 2000, null);
  let { root } = mount([zero, missing]);
  const timeChart = root.querySelector('#chart-pareto');
  assert.match(timeChart.querySelector('[data-model-id="zero-time"]').getAttribute('aria-label'), /Mean time per task: 0s/);
  assert.match(timeChart.querySelector('[data-model-id="unknown-time"]').getAttribute('aria-label'), /Mean time per task: n\/a/);
  metricButton(timeChart, 'time').dispatchEvent({ type: 'click' });
  assert.match(timeChart.textContent, /0s mean time per task for zero-time/);
  assert.equal(timeChart.querySelectorAll('svg').length, 0);

  ({ root } = mount([entry('zero-only', 100, 1, 0, 20)]));
  const tokensChart = root.querySelector('#chart-pareto');
  metricButton(tokensChart, 'tokens').dispatchEvent({ type: 'click' });
  assert.match(tokensChart.textContent, /zero token totals for zero-only/);
  assert.equal(tokensChart.querySelectorAll('svg').length, 0);
});

test('heatmaps distinguish an observed zero from an unavailable value', () => {
  const zero = entry('zero-regime', 100, 1);
  zero.analysis.regime_g_srr = { R1: 0 };
  const { root } = mount([zero], { width: 390 });
  const chart = root.querySelector('#chart-marginals');
  const row = chart.querySelector('.chart-model-row');
  assert.equal(row.getAttribute('role'), 'button');
  assert.match(row.getAttribute('aria-label'), /Zero-trust: 0\.0%/);
  assert.match(row.getAttribute('aria-label'), /Multi-affiliate: n\/a/);

  const zeroCell = row.querySelector('.chart-heat-zero');
  assert.equal(zeroCell.tagName, 'rect');
  assert.equal(zeroCell.getAttribute('fill'), 'var(--seq-1)');
  assert.equal(row.querySelectorAll('.chart-heat-unavailable').length, 3);
  assert.ok(row.querySelectorAll('.chart-heat-unavailable').every((cell) => cell.tagName === 'rect' && cell.getAttribute('stroke-dasharray') === '2 2'));
  const cellLabels = row.querySelectorAll('.chart-cell-label');
  assert.equal(cellLabels[0].textContent, '0%');
  assert.equal(cellLabels[1].textContent, '—');
  const legend = chart.querySelector('.chart-heatmap-legend');
  assert.match(legend.textContent, /0% observed/);
  assert.match(legend.textContent, /Not reported/);

  const environmentCanvas = chart.querySelector('.chart-canvas');
  assert.equal(environmentCanvas.classList.contains('heatmap-scroll'), false);
  assert.equal(environmentCanvas.getAttribute('tabindex'), null);
  assert.equal(chart.querySelector('.chart-scroll-cue'), null);
  assert.equal(chart.querySelector('svg').getAttribute('width'), '390');
});

test('narrow heatmaps expose a keyboard-scrollable region and cue only when they overflow', () => {
  const zero = entry('zero-regime', 100, 1);
  zero.analysis.regime_g_srr = { R1: 0 };
  const { root, dom } = mount([zero], { width: 320 });
  const environmentChart = root.querySelector('#chart-marginals');
  const canvas = environmentChart.querySelector('.chart-canvas');
  const cue = environmentChart.querySelector('.chart-scroll-cue');
  assert.ok(cue);
  assert.equal(cue.textContent, 'Scroll horizontally to see all 4 enterprise environments.');
  assert.equal(canvas.getAttribute('role'), 'region');
  assert.equal(canvas.getAttribute('tabindex'), '0');
  assert.equal(canvas.getAttribute('aria-describedby'), cue.getAttribute('id'));
  assert.equal(canvas.classList.contains('heatmap-scroll'), true);
  assert.equal(environmentChart.querySelector('svg').getAttribute('width'), '370');
  assert.equal(root.querySelector('#chart-categories').querySelector('.chart-scroll-cue'), null);

  root.rect.width = 390;
  dom.resize();
  assert.equal(environmentChart.querySelector('.chart-scroll-cue'), null);
  assert.equal(canvas.classList.contains('heatmap-scroll'), false);
  assert.equal(canvas.getAttribute('tabindex'), null);
  assert.equal(canvas.getAttribute('role'), null);
  assert.equal(canvas.getAttribute('aria-describedby'), null);
});

test('log axes remain finite for the smallest and largest finite positive values', () => {
  const { root } = mount([
    entry('tiny', -5, Number.MIN_VALUE, Number.MIN_VALUE, Number.MIN_VALUE),
    entry('huge', 500, Number.MAX_VALUE, Number.MAX_VALUE, Number.MAX_VALUE),
  ]);
  const chart = root.querySelector('#chart-pareto');
  for (const metric of ['cost', 'tokens', 'time']) {
    metricButton(chart, metric).dispatchEvent({ type: 'click' });
    for (const mark of chart.querySelectorAll('.chart-mark')) {
      const circle = mark.querySelector('circle');
      assert.ok(Number.isFinite(Number(circle.getAttribute('cx'))));
      assert.ok(Number.isFinite(Number(circle.getAttribute('cy'))));
    }
  }
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
