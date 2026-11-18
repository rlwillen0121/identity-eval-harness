import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { columnDefinitions, compareModels, foldVendorChips, identityCellMarkup, matchesModelQuery, modelCellsMarkup, mount, sortComparator, toggleVendorSelection, whisker } from '../assets/scripts/table.js';
import { fieldMedians, stateAccuracySummary } from '../assets/scripts/detail.js';
import { leaderboardLede } from '../assets/scripts/main.js';
import { groupUpdates, VISIBLE_UPDATE_COUNT } from '../assets/scripts/updates.js';
import { END_TO_END_INTERVAL_METHOD, endToEndPct, escapeHTML, int, pct, tokens } from '../assets/scripts/format.js';
import { buildLeaderboardData, serializeLeaderboardData } from '../build.mjs';
import { makeStore } from './dom-stub.mjs';

const data = JSON.parse(readFileSync(new URL('../data/leaderboard.json', import.meta.url), 'utf8'));
const sortableColumns = ['rank', 'name', 'g_srr', 'sem_srr_commit', 'categories', 'safety', 'output_tokens', 'cost_usd'];

function median(values) {
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

test('leaderboard.json contains only complete 144/144 models, all 7 baselines, and matches build.mjs', () => {
  assert.equal(data.schema, 2);
  assert.equal(data.benchmark, 'IdentityBench v5');
  assert.equal(data.suite.id, 'v5_core');
  assert.equal(data.suite.episodes, 48);
  assert.equal(data.suite.repeats, 3);
  assert.equal(data.suite.slots_per_run, 144);
  assert.equal(data.categories.length, 7);
  assert.equal(data.families.length, 12);
  assert.equal(data.models.length, 18);
  assert.equal(data.baselines.length, 7);
  for (const model of data.models) {
    assert.equal(model.is_complete, true);
    assert.equal(model.observed_slots, 144);
    assert.equal(model.planned_slots, 144);
    assert.equal(model.failures.provider_errors, 0);
  }
  const rebuilt = serializeLeaderboardData(buildLeaderboardData());
  const committed = readFileSync(new URL('../data/leaderboard.json', import.meta.url), 'utf8');
  assert.equal(committed, rebuilt);
});

test('table interval, columns, and cost titles match v5 methods', () => {
  const columns = columnDefinitions(data);
  assert.deepEqual(columns.map(([key, label]) => [key, label]), [
    ['rank', '#'], ['name', 'Model'], ['g_srr', 'End to end'], ['sem_srr_commit', 'State accuracy'],
    ['categories', 'By category'], ['safety', 'Safety'], ['output_tokens', 'Total tokens'], ['cost_usd', 'Cost'],
  ]);
  const titles = new Map(columns.map(([key, , title]) => [key, title]));
  assert.match(titles.get('rank'), /^Rank by .*model name\.$/);
  assert.ok(titles.get('g_srr').includes(END_TO_END_INTERVAL_METHOD));
  assert.match(titles.get('g_srr'), /144 slots/);
  assert.match(titles.get('sem_srr_commit'), /Sem-SRR@commit/);
  assert.match(titles.get('categories'), /7 v5 governance categories/);
  assert.match(titles.get('safety'), /OVERGRANT@commit/);
  assert.match(titles.get('output_tokens'), /output and reasoning tokens/);
  assert.match(titles.get('output_tokens'), /144 slots/);
  assert.match(titles.get('cost_usd'), /Billed amount when reported/);
  assert.match(titles.get('cost_usd'), /Local runs have no cost/);
});

test('end-to-end cells include slot counts and hide whiskers on mobile', () => {
  const zeroWhisker = whisker({ g_srr: 0, g_srr_ci: [0, 0], g_srr_slots: 0 }, 144);
  assert.match(zeroWhisker, /resolution-number muted">0%<\/span>/);
  assert.match(zeroWhisker, /resolution-count muted" aria-label="0 of 144 slots resolved end to end">0<\/span>/);
  assert.match(zeroWhisker, /95% interval for end-to-end rate.*0% to 0%/);
  assert.doesNotMatch(zeroWhisker, /0\.0%/);

  const sample = data.models[0];
  const cells = modelCellsMarkup(sample, data);
  const columnMarkers = [
    '<td class="rank-cell">', '<td class="identity-cell"><button', '<td class="resolution-cell">',
    '<td class="desktop-col tasks-cell">', '<td class="desktop-col"><div class="category-bars">',
    '<td class="desktop-col safety-cell">', '<td class="desktop-col tokens-cell"', '<td class="desktop-col cost-cell"',
  ].map((marker) => cells.indexOf(marker));
  assert.ok(columnMarkers.every((position) => position >= 0));
  assert.deepEqual(columnMarkers, [...columnMarkers].sort((a, b) => a - b), 'tbody uses the 8-column order');
  assert.match(cells, /class="task-count">\d+<\/span>/);
  assert.ok(cells.includes(`>${tokens(sample.output_tokens)}</td>`));

  const identityCell = identityCellMarkup(sample);
  assert.ok(identityCell.indexOf('class="model-name"') < identityCell.indexOf('class="model-attribution"'));
  assert.ok(identityCell.includes(`class="effort-tag">${escapeHTML(sample.effort)}</span>`));

  const css = readFileSync(new URL('../assets/styles/table.css', import.meta.url), 'utf8');
  const mobile = css.split('@media (max-width: 759px)')[1];
  assert.match(css, /\.model-attribution \{ display: contents; \}/);
  assert.match(mobile, /\.model-name \{ display: block;/);
  assert.match(mobile, /\.model-attribution \{ display: block; \}/);
  assert.match(mobile, /\.desktop-col \{ display: none; \}/);
  assert.match(mobile, /\.ci-whisker \{ display: none; \}/);
});

test('exact leaderboard lede and detailed state accuracy summary use formatted slot counts', () => {
  assert.equal(
    leaderboardLede({ suite: { slots_per_run: 144, episodes: 48, repeats: 3 } }),
    'Ranked across 144 slots (48 scenarios × 3 repeats) by strict two-phase resolution, committed state accuracy, and safety.',
  );
  const model = data.models[0];
  assert.equal(
    stateAccuracySummary(model, data.suite.slots_per_run),
    `${int(model.sem_srr_slots)} of ${int(data.suite.slots_per_run)} slots (${pct(model.sem_srr_commit)})`,
  );
});

test('every sortable column sorts independently in both directions', () => {
  const fixture = [
    { id: 'low', rank: 3, name: 'Alpha', g_srr: 0.1, sem_srr_commit: 0.2, categories: { a: { pass_count: 1 } }, overgrant_rate: 0.01, avail_breach_rate: 0.01, output_tokens: 1000, cost_usd: 1, cost_basis: 'retail_api' },
    { id: 'middle', rank: 1, name: 'Bravo', g_srr: 0.2, sem_srr_commit: 0.4, categories: { a: { pass_count: 2 } }, overgrant_rate: 0.02, avail_breach_rate: 0.02, output_tokens: 2000, cost_usd: 2, cost_basis: 'retail_api' },
    { id: 'high', rank: 2, name: 'Charlie', g_srr: 0.3, sem_srr_commit: 0.6, categories: { a: { pass_count: 3 } }, overgrant_rate: 0.03, avail_breach_rate: 0.03, output_tokens: 3000, cost_usd: 3, cost_basis: 'retail_api' },
  ];
  const originalIds = data.models.map((model) => model.id);
  for (const key of sortableColumns) {
    const ascending = key === 'rank' ? ['middle', 'high', 'low'] : ['low', 'middle', 'high'];
    const descending = key === 'rank' ? ['low', 'high', 'middle'] : ['high', 'middle', 'low'];
    assert.deepEqual([...fixture].sort(sortComparator(key, 'asc')).map((model) => model.id), ascending, `${key} ascending`);
    assert.deepEqual([...fixture].sort(sortComparator(key, 'desc')).map((model) => model.id), descending, `${key} descending`);
    assert.equal([...data.models].sort(sortComparator(key, 'asc')).length, data.models.length);
    assert.equal([...data.models].sort(sortComparator(key, 'desc')).length, data.models.length);
  }
  assert.deepEqual(data.models.map((model) => model.id), originalIds);
});

test('rank is the ascending tie-break and local cost sorts as zero', () => {
  const shared = { name: 'Equal', g_srr: 0.5, sem_srr_commit: 0.5, categories: { a: { pass_count: 20 } }, overgrant_rate: 0.1, avail_breach_rate: 0.1, output_tokens: 5000, cost_usd: 10, cost_basis: 'retail_api' };
  for (const key of sortableColumns) {
    for (const direction of ['asc', 'desc']) {
      const first = { ...shared, id: 'rank-two', rank: 2 };
      const second = { ...shared, id: 'rank-one', rank: 1 };
      if (key === 'rank') first.rank = second.rank = 1;
      assert.ok(compareModels(second, first, key, direction) <= 0, `${key} ${direction} rank tie-break`);
    }
  }
  const local = { ...shared, id: 'local', rank: 2, cost_usd: 999, cost_basis: 'local' };
  const paid = { ...shared, id: 'paid', rank: 1, cost_usd: 1, cost_basis: 'retail_api' };
  assert.ok(compareModels(local, paid, 'cost_usd', 'asc') < 0);
});

test('vendor chips reconcile model counts and fold singleton vendors into Other', () => {
  const before = structuredClone(data.vendors);
  const chips = foldVendorChips(data.vendors);
  const visible = data.vendors.filter((vendor) => vendor.count >= 2);
  const singletons = data.vendors.filter((vendor) => vendor.count < 2);
  const other = chips.at(-1);

  assert.equal(data.vendors.reduce((sum, vendor) => sum + vendor.count, 0), data.models.length);
  assert.deepEqual(chips.slice(0, -1).map((chip) => chip.id), visible.map((vendor) => vendor.id));
  assert.equal(chips.length, visible.length + 1);
  assert.deepEqual(other.vendorIds, singletons.map((vendor) => vendor.id));
  assert.equal(other.label, 'Other');
  assert.equal(other.count, singletons.reduce((sum, vendor) => sum + vendor.count, 0));
  assert.equal(chips.reduce((sum, chip) => sum + chip.count, 0), data.models.length);
  assert.deepEqual(data.vendors, before);

  const allFolded = toggleVendorSelection(new Set(), other);
  assert.deepEqual(allFolded, new Set(other.vendorIds));
  assert.deepEqual(toggleVendorSelection(allFolded, { id: '', kind: 'all' }), new Set());
});

test('model query matching is case-insensitive across name and vendor label', () => {
  const model = { id: 'private-model-id', name: 'Example Model', vendor_label: 'Sample Vendor' };
  assert.equal(matchesModelQuery(model, 'example'), true);
  assert.equal(matchesModelQuery(model, 'SAMPLE VENDOR'), true);
  assert.equal(matchesModelQuery(model, 'private-model-id'), false);
});

test('all seven category and twelve family medians use every model field rate', () => {
  const medians = fieldMedians(data);
  assert.deepEqual(Object.keys(medians.categories), data.categories.map((category) => category.id));
  assert.equal(Object.keys(medians.categories).length, 7);
  assert.equal(medians.families.length, 12);

  for (const category of data.categories) {
    const rates = data.models.map((model) => model.categories[category.id].pass_count / data.suite.slots_per_run);
    assert.equal(medians.categories[category.id], median(rates), `${category.id} median`);
  }
  for (let i = 0; i < data.families.length; i++) {
    const rates = data.models.map((model) => model.families[i]);
    assert.equal(medians.families[i], median(rates), `family ${i + 1} median`);
  }
});

test('updates follow history date membership, newest-first groups, and six-plus fold count', () => {
  const { groups, earliestDate } = groupUpdates(data);
  const historyDates = data.history.map((entry) => entry.date);
  assert.equal(VISIBLE_UPDATE_COUNT, 6);
  assert.deepEqual(groups.map((group) => group.date), [...historyDates].sort((a, b) => b.localeCompare(a)));
  assert.equal(earliestDate, [...historyDates].sort((a, b) => a.localeCompare(b))[0]);
  assert.ok(groups.some((group) => group.models.length > VISIBLE_UPDATE_COUNT));
  assert.deepEqual(new Set(groups.flatMap((group) => group.models.map((model) => model.id))), new Set(data.history.flatMap((entry) => entry.models)));
});

test('every vendor on the leaderboard has a mark, and unknown vendors fall back to an escaped monogram', async () => {
  const { hasVendorLogo, vendorLogo, vendorLogoBody } = await import('../assets/scripts/logos.js');
  for (const vendor of data.vendors) assert.ok(hasVendorLogo(vendor.id), `missing mark for ${vendor.id}`);
  for (const model of data.models) {
    const cell = identityCellMarkup(model);
    assert.match(cell, /<svg class="vendor-logo" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"/);
    assert.ok(cell.includes(escapeHTML(model.vendor_label)), 'the vendor name stays in text beside its mark');
  }
  assert.equal(hasVendorLogo('constructor'), false);
  assert.match(vendorLogoBody('newco', '<New>'), /<text[^>]*>&lt;<\/text>/);
  assert.match(vendorLogo('newco', 'Newco'), />N<\/text><\/svg>$/);
  assert.doesNotMatch(vendorLogoBody('openai'), /<text/);
});

test('self-hosted fonts exist, CSP stays self-only, and study disclaimers are present', () => {
  const site = new URL('../', import.meta.url);
  const tokensCss = readFileSync(new URL('assets/styles/tokens.css', site), 'utf8');
  const fontUrls = [...tokensCss.matchAll(/url\("\.\.\/fonts\/([^"]+)"\)/g)].map((match) => match[1]);
  assert.equal(fontUrls.length, 3);
  for (const file of fontUrls) assert.ok(existsSync(new URL(`assets/fonts/${file}`, site)), file);

  const page = readFileSync(new URL('index.html', site), 'utf8');
  assert.match(page, /<span class="version">v5 · core<\/span>/);
  assert.doesNotMatch(page, /<style[\s>]/);
  assert.doesNotMatch(page, /<script(?![^>]*\ssrc="assets\/)[^>]*>/);
  assert.doesNotMatch(page, /<link[^>]+href="https?:\/\/(?!identitybench\.ai\/)/);
  assert.match(page, /<p class="study-note">[^<]*Independent exploratory research study[^<]*synthetic environments[^<]*not an official or vendor-endorsed benchmark\.?<\/p>/i);
  assert.match(page, /<footer class="site-footer">[\s\S]*independent, exploratory research study[\s\S]*synthetic enterprise environments[\s\S]*not an official standards-body or vendor-endorsed benchmark[\s\S]*<\/footer>/i);
});

test('mounted table spans all eight columns, renders baselines toggle, and renders distinct terminations in detail', () => {
  const stubEl = () => ({ innerHTML: '', value: '', textContent: '', addEventListener() {}, replaceChildren() { this.innerHTML = ''; }, querySelectorAll: () => [], querySelector: () => stubEl() });
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
  const root = {
    innerHTML: '',
    querySelector: (selector) => elements[selector] || null,
    querySelectorAll: () => [],
  };
  const previousDocument = globalThis.document;
  globalThis.document = { activeElement: null };
  try {
    const store = makeStore({ selected: data.models[0].id });
    const unmount = mount(root, data, store);
    assert.match(root.innerHTML, /<summary>Show reference &amp; baselines<\/summary>/);
    for (const b of data.baselines) {
      assert.ok(root.innerHTML.includes(escapeHTML(b.name)), `missing baseline ${b.name}`);
    }
    assert.match(root.innerHTML, /<th scope="col" data-key="output_tokens" class="desktop-col">/);
    assert.match(body.innerHTML, /<tr class="detail-row"><td colspan="8"><div class="model-detail"/);
    assert.ok(detail.innerHTML.includes('7 Governance categories'));
    assert.ok(detail.innerHTML.includes('12 Coupling families (F01–F12)'));
    assert.ok(detail.innerHTML.includes('Parse failures'));
    assert.ok(detail.innerHTML.includes('Turn limits'));
    assert.ok(detail.innerHTML.includes('Output-budget stops'));
    assert.ok(detail.innerHTML.includes('Context-limit stops'));
    assert.ok(detail.innerHTML.includes(`${tokens(data.models[0].output_tokens)} (${int(data.models[0].output_tokens)} total`));

    store.set({ selected: null, query: '__no_such_model__' });
    assert.equal(body.innerHTML, '<tr class="empty-row"><td colspan="8">No models match these filters. Clear the search or choose All vendors.</td></tr>');
    unmount();
  } finally {
    globalThis.document = previousDocument;
  }
});
