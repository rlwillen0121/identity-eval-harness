import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { hasVendorLogo } from '../../../site/assets/scripts/logos.js';
import { escapeHTML, int, pct } from '../../../site/assets/scripts/format.js';
import { v5ColumnDefinitions, v5FailureSummaryMarkup, v5ModelCellsMarkup, v5SortComparator, mount, mountIncomplete } from '../../../site/assets/scripts/v5-table.js';
import { v5CalloutSummary, v5LeaderboardLede } from '../../../site/assets/scripts/v5-main.js';
import * as v5Resolution from '../../../site/assets/scripts/charts/v5-resolution.js';
import * as v5Cost from '../../../site/assets/scripts/charts/v5-cost.js';
import * as v5Outcomes from '../../../site/assets/scripts/charts/v5-outcomes.js';
import * as v5Categories from '../../../site/assets/scripts/charts/v5-categories.js';
import { installDOM, makeStore } from './dom-stub.mjs';

const v5Data = JSON.parse(readFileSync(new URL('../../../site/data/v5-preliminary.json', import.meta.url), 'utf8'));

test('every vendor across all 24 v5 configurations has a mark in logos.js', () => {
  const allRuns = [...v5Data.models, ...v5Data.incomplete_runs];
  assert.equal(allRuns.length, 24);
  for (const run of allRuns) {
    assert.ok(hasVendorLogo(run.vendor), `missing vendor mark for ${run.vendor} (${run.id})`);
  }
});

test('v5.html and index.html link cleanly and preserve CSP and self-hosted fonts', () => {
  const site = new URL('../../../site/', import.meta.url);
  const v5Html = readFileSync(new URL('v5.html', site), 'utf8');
  const indexHtml = readFileSync(new URL('index.html', site), 'utf8');

  assert.match(indexHtml, /href="v5\.html"/);
  assert.match(v5Html, /href="\.\/"/);

  assert.doesNotMatch(v5Html, /<style[\s>]/);
  assert.doesNotMatch(v5Html, /<script(?![^>]*\ssrc="assets\/)[^>]*>/);
  assert.doesNotMatch(v5Html, /<link[^>]+href="https?:\/\/(?!identitybench\.ai\/)/);

  const tokens = readFileSync(new URL('assets/styles/tokens.css', site), 'utf8');
  const fontUrls = [...tokens.matchAll(/url\("\.\.\/fonts\/([^"]+)"\)/g)].map((m) => m[1]);
  const preloads = [...v5Html.matchAll(/<link rel="preload" href="([^"]+)" as="font" type="font\/woff2" crossorigin>/g)].map((m) => m[1]);
  assert.ok(preloads.length >= 1);
  for (const href of preloads) {
    assert.ok(existsSync(new URL(href, site)), href);
    assert.ok(fontUrls.includes(href.split('/').pop()));
  }

  assert.match(v5Html, /Preliminary Preview/i);
  assert.match(v5Html, /Provider-Incomplete Runs \(Not Comparable \/ Excluded from Ranking\)/);
  assert.match(v5Html, /<p class="study-note">[^<]*Independent exploratory research study/i);
  assert.match(v5Html, /<footer class="site-footer">[\s\S]*independent, exploratory research study[\s\S]*<\/footer>/i);

  assert.match(v5LeaderboardLede(v5Data), /18 protocol-complete/);
  assert.match(v5CalloutSummary(v5Data), /24\/24 summaries & reports/);
});

test('v5 failure markup keeps provider errors, parse failures, and turn limits separate', () => {
  const mixed = v5FailureSummaryMarkup({
    provider_errors: 0,
    parse_failures: 23,
    turn_limits: 121,
    output_limits: 0,
    context_limits: 0,
  });
  assert.match(mixed, /23 parse/);
  assert.match(mixed, /121 turn/);
  assert.doesNotMatch(mixed, /prov/);

  const prov = v5FailureSummaryMarkup({
    provider_errors: 140,
    parse_failures: 1,
    turn_limits: 1,
    output_limits: 0,
    context_limits: 2,
  });
  assert.match(prov, /140 prov/);
  assert.match(prov, /1 parse/);
  assert.match(prov, /1 turn/);
  assert.match(prov, /2 ctx/);
  assert.equal(v5FailureSummaryMarkup({}), '<span class="failure-zero">0</span>');
});

test('v5 primary table sorts cleanly, renders 7 categories, and separates incomplete runs', () => {
  const cols = v5ColumnDefinitions(v5Data);
  assert.equal(cols.length, 8);
  assert.deepEqual(cols.map(([k]) => k), ['rank', 'name', 'g_srr', 'sem_srr_commit', 'categories', 'safety', 'failures', 'cost_usd']);

  const bySem = [...v5Data.models].sort(v5SortComparator('sem_srr_commit', 'desc'));
  assert.equal(bySem[0].name, 'Claude Sonnet 5.5');

  const sample = v5Data.models[0];
  const rowHtml = v5ModelCellsMarkup(sample, v5Data);
  assert.equal((rowHtml.match(/class="category-track"/g) || []).length, 7);
  assert.ok(rowHtml.includes('Agent governance'));

  const stubEl = () => ({
    innerHTML: '', value: '', textContent: '',
    addEventListener() {}, replaceChildren() { this.innerHTML = ''; },
    querySelectorAll: () => [], querySelector: () => stubEl(),
  });
  const detail = stubEl();
  const body = {
    innerHTML: '',
    addEventListener() {},
    querySelectorAll: () => [],
    querySelector: (selector) => (selector === '.model-detail' && body.innerHTML.includes('class="model-detail"') ? detail : null),
  };
  const elements = {
    '.leaderboard-table > tbody': body,
    '.model-search': stubEl(),
    '.model-count': stubEl(),
    '.vendor-controls': stubEl(),
    thead: stubEl(),
  };
  const root = { innerHTML: '', querySelector: (sel) => elements[sel] || null, querySelectorAll: () => [] };

  const previousDocument = globalThis.document;
  globalThis.document = { activeElement: null };
  try {
    const store = makeStore({ selected: sample.id });
    const unmount = mount(root, v5Data, store);
    assert.match(body.innerHTML, /<tr class="detail-row"><td colspan="8"><div class="model-detail"/);
    assert.ok(detail.innerHTML.includes('Agent governance'));
    assert.ok(detail.innerHTML.includes('OVERGRANT@commit'));

    store.set({ selected: null, query: '__missing_model__' });
    assert.match(body.innerHTML, /<tr class="empty-row"><td colspan="8">/);
    unmount();
  } finally {
    globalThis.document = previousDocument;
  }

  const incRoot = { innerHTML: '' };
  mountIncomplete(incRoot, v5Data);
  for (const inc of v5Data.incomplete_runs) {
    assert.ok(incRoot.innerHTML.includes(escapeHTML(inc.run_id)), `missing ${inc.run_id}`);
    assert.ok(incRoot.innerHTML.includes(int(inc.failures.provider_errors)));
    assert.ok(incRoot.innerHTML.includes(pct(inc.provisional_bounds[1])));
  }
  assert.match(incRoot.innerHTML, /UNMEASURED/);
  assert.doesNotMatch(incRoot.innerHTML, /<th scope="col">#<\/th>/);
});

const v5Charts = { v5Resolution, v5Cost, v5Outcomes, v5Categories };
const chartStates = {
  rest: {},
  hover: { hover: v5Data.models[0].id },
  selected: { selected: v5Data.models[0].id },
  filtered: { vendors: new Set([v5Data.models[0].vendor]) },
};
const validAttributes = (root) => {
  for (const node of root.querySelectorAll('*')) {
    for (const { name, value } of node.attributes) {
      assert.doesNotMatch(value, /NaN|undefined|Infinity/, `${node.tagName}.${name}=${value}`);
    }
  }
};

for (const [name, chart] of Object.entries(v5Charts)) {
  for (const width of [328, 560, 1120]) {
    for (const [stateName, state] of Object.entries(chartStates)) {
      test(`${name} mounts at ${width}px in ${stateName} state`, () => {
        const dom = installDOM();
        const root = dom.root(width);
        const store = makeStore(state);
        const destroy = chart.mount(root, v5Data, store);
        validAttributes(root);
        const svgs = root.querySelectorAll('svg');
        assert.equal(svgs.length, 1);
        assert.equal(svgs[0].getAttribute('role'), 'group');
        assert.ok(svgs[0].getAttribute('aria-label')?.trim());
        const marks = root.querySelectorAll('[data-model-id]');
        assert.equal(new Set(marks.map((n) => n.getAttribute('data-model-id'))).size, v5Data.models.length);
        dom.resize(root, 328);
        validAttributes(root);
        destroy();
      });
    }
  }
}
