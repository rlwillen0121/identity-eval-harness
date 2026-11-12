import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { columnDefinitions, compareModels, foldVendorChips, identityCellMarkup, matchesModelQuery, modelCellsMarkup, sortComparator, toggleVendorSelection, whisker } from '../../../site/assets/scripts/table.js';
import { fieldMedians, tasksSolvedSummary } from '../../../site/assets/scripts/detail.js';
import { leaderboardLede } from '../../../site/assets/scripts/main.js';
import { groupUpdates, VISIBLE_UPDATE_COUNT } from '../../../site/assets/scripts/updates.js';
import { END_TO_END_INTERVAL_METHOD, endToEndPct, escapeHTML, pct } from '../../../site/assets/scripts/format.js';

const data = JSON.parse(readFileSync(new URL('../../../site/data/leaderboard.json', import.meta.url), 'utf8'));
const sortableColumns = ['rank', 'name', 'g_srr', 'tasks_solved', 'categories', 'unsafe_rate', 'cost_usd'];

function median(values) {
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

test('table interval and cost titles match the published methods', () => {
  const columns = columnDefinitions(data);
  assert.deepEqual(columns.map(([key, label]) => [key, label]), [
    ['rank', '#'], ['name', 'Model'], ['g_srr', 'End to end'], ['tasks_solved', 'Tasks solved'],
    ['categories', 'By category'], ['unsafe_rate', 'Unsafe'], ['cost_usd', 'Cost'],
  ]);
  const titles = new Map(columns.map(([key, , title]) => [key, title]));
  assert.match(titles.get('rank'), /^Rank by .*model name\.$/);
  assert.equal((titles.get('rank').match(/\./g) || []).length, 1);
  assert.ok(titles.get('g_srr').includes(END_TO_END_INTERVAL_METHOD));
  assert.match(titles.get('g_srr'), /95% interval/);
  assert.match(titles.get('g_srr'), /every wave correct/);
  assert.match(titles.get('g_srr'), /nothing missed/);
  assert.match(titles.get('g_srr'), /count is scenarios fully resolved/);
  assert.match(titles.get('cost_usd'), /billed amount when reported/);
  assert.match(titles.get('cost_usd'), /retail token prices/);
  assert.match(titles.get('cost_usd'), /Local runs have no cost/);
});

test('end-to-end cells include the muted perfect-scenario count and hide the whisker on mobile', () => {
  const zeroWhisker = whisker({ g_srr: 0, g_srr_ci: [0, 0], outcomes: { perfect: 0 } }, 180);
  assert.match(zeroWhisker, /resolution-number muted">0%<\/span>/);
  assert.match(zeroWhisker, /resolution-count muted" aria-label="0 of 180 scenarios resolved end to end">0<\/span>/);
  assert.match(zeroWhisker, /95% interval for end-to-end rate.*0% to 0%/);
  assert.doesNotMatch(zeroWhisker, /0\.0%/);

  const sample = data.models[0];
  const cells = modelCellsMarkup(sample, data, Math.max(...data.models.map((model) => model.unsafe_rate)));
  const columnMarkers = [
    '<td class="rank-cell">', '<td class="identity-cell"><button', '<td class="resolution-cell">',
    '<td class="desktop-col tasks-cell">', '<td class="desktop-col"><div class="category-bars">',
    '<td class="desktop-col unsafe-cell">', '<td class="desktop-col cost-cell"',
  ].map((marker) => cells.indexOf(marker));
  assert.ok(columnMarkers.every((position) => position >= 0));
  assert.deepEqual(columnMarkers, [...columnMarkers].sort((a, b) => a - b), 'tbody uses the published column order');
  assert.match(cells, /class="task-count">\d[\d,]*<\/span>/);

  const passingModel = data.models.find((model) => model.screen_clear);
  const identityCell = identityCellMarkup(passingModel, data.screen);
  assert.ok(identityCell.indexOf('class="model-name"') < identityCell.indexOf('class="model-attribution"'));
  for (const check of data.screen) assert.ok(identityCell.includes(escapeHTML(check.label)), check.label);

  const css = readFileSync(new URL('../../../site/assets/styles/table.css', import.meta.url), 'utf8');
  const mobile = css.split('@media (max-width: 759px)')[1];
  assert.match(css, /\.model-attribution \{ display: contents; \}/);
  assert.match(mobile, /\.model-name \{ display: block;/);
  assert.match(mobile, /\.model-attribution \{ display: block; \}/);
  assert.match(mobile, /\.vendor-label \{ display: inline; margin-left: 0; \}/);
  assert.match(mobile, /\.desktop-col \{ display: none; \}/);
  assert.match(mobile, /\.ci-whisker \{ display: none; \}/);
  assert.match(mobile, /\.leaderboard-table th:nth-child\(3\) \{ width: 40%; \}/);
  assert.doesNotMatch(mobile, /\.resolution-cell\s*\{[^}]*display:\s*none/);
  assert.match(css, /\.vendor-label \{ display: block; margin-left: 0; \}/);
});

test('exact leaderboard lede and detailed task total use formatted suite counts', () => {
  assert.equal(leaderboardLede({ suite: { scenarios: 1180, tasks: 1080 } }), 'Ranked by the 1,180 scenarios each model resolves end to end: every wave correct, nothing unsafe, nothing missed.');
  const model = data.models.find((item) => item.tasks_solved > 0);
  assert.equal(tasksSolvedSummary(model, data.suite.tasks), `${new Intl.NumberFormat('en-US').format(model.tasks_solved)} of ${new Intl.NumberFormat('en-US').format(data.suite.tasks)} tasks (${pct(model.tasks_rate)})`);
});

test('every sortable column has independent low-to-high orders in both directions', () => {
  const fixture = [
    { id: 'low', rank: 3, name: 'Alpha', tasks_solved: 10, categories: { a: 0, b: 1 }, g_srr: 0.1, unsafe_rate: 0.01, cost_usd: 1 },
    { id: 'middle', rank: 1, name: 'Bravo', tasks_solved: 20, categories: { a: 1, b: 1 }, g_srr: 0.2, unsafe_rate: 0.02, cost_usd: 2 },
    { id: 'high', rank: 2, name: 'Charlie', tasks_solved: 30, categories: { a: 2, b: 1 }, g_srr: 0.3, unsafe_rate: 0.03, cost_usd: 3 },
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

test('rank is the ascending tie-break in both directions and equal ties stay stable', () => {
  const shared = { name: 'Equal', tasks_solved: 50, categories: { a: 20, b: 30 }, g_srr: 0.5, unsafe_rate: 0.1, cost_usd: 10 };
  for (const key of sortableColumns) {
    for (const direction of ['asc', 'desc']) {
      const first = { ...shared, id: 'rank-two', rank: 2 };
      const second = { ...shared, id: 'rank-one', rank: 1 };
      if (key === 'rank') first.rank = second.rank = 1;
      assert.ok(compareModels(second, first, key, direction) <= 0, `${key} ${direction} rank tie-break`);
    }
  }

  for (const key of sortableColumns) {
    for (const direction of ['asc', 'desc']) {
      const first = { ...shared, id: 'first', rank: 1 };
      const second = { ...shared, id: 'second', rank: 1 };
      assert.equal(compareModels(first, second, key, direction), 0, `${key} ${direction} equal comparator result`);
      assert.deepEqual([second, first].sort(sortComparator(key, direction)), [second, first], `${key} ${direction} stable tie`);
    }
  }
});

test('vendor chips preserve data order, reconcile model counts, and fold singleton vendors into Other', () => {
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
  assert.ok([...allFolded].every((id) => data.vendors.some((vendor) => vendor.id === id)));
  const mixed = toggleVendorSelection(new Set([visible[0].id, singletons[0].id]), other);
  assert.deepEqual(mixed, new Set([visible[0].id, ...other.vendorIds]));
  assert.deepEqual(toggleVendorSelection(mixed, other), new Set([visible[0].id]));
  assert.deepEqual(toggleVendorSelection(mixed, { id: '', kind: 'all' }), new Set());
});

test('model query matching is case-insensitive across name and vendor label', () => {
  const model = { id: 'private-model-id', name: 'Example Model', vendor_label: 'Sample Vendor' };
  assert.equal(matchesModelQuery(model, 'example'), true);
  assert.equal(matchesModelQuery(model, 'SAMPLE VENDOR'), true);
  assert.equal(matchesModelQuery(model, '  vendor  '), true);
  assert.equal(matchesModelQuery(model, ''), true);
  assert.equal(matchesModelQuery(model, 'private-model-id'), false);
  assert.equal(matchesModelQuery(model, 'missing'), false);
});

test('all six category and five optimization medians use every model field rate', () => {
  const medians = fieldMedians(data);
  assert.deepEqual(Object.keys(medians.categories), data.categories.map((category) => category.id));
  assert.deepEqual(Object.keys(medians.opt_tasks), data.opt_tasks.map((task) => task.id));
  assert.equal(Object.keys(medians.categories).length, 6);
  assert.equal(Object.keys(medians.opt_tasks).length, 5);

  for (const category of data.categories) {
    const rates = data.models.map((model) => model.categories[category.id] / data.suite.scenarios);
    assert.equal(medians.categories[category.id], median(rates), `${category.id} median`);
  }
  for (const task of data.opt_tasks) {
    const rates = data.models.map((model) => {
      const field = model.opt[task.id];
      return field.total > 0 ? field.hits / field.total : 0;
    });
    assert.equal(medians.opt_tasks[task.id], median(rates), `${task.id} median`);
  }
  for (const value of [...Object.values(medians.categories), ...Object.values(medians.opt_tasks)]) {
    assert.ok(Number.isFinite(value) && value >= 0 && value <= 1);
  }

  const partial = fieldMedians({
    suite: { scenarios: 2 },
    categories: [{ id: 'category' }],
    opt_tasks: [{ id: 'task' }],
    models: [
      { categories: { category: 0 }, opt: { task: { hits: 0, total: 0 } } },
      { categories: { category: 1 }, opt: { task: { hits: 1, total: 1 } } },
    ],
  });
  assert.equal(partial.categories.category, 0.25);
  assert.equal(partial.opt_tasks.task, 0.5);
});

test('updates follow history date membership, newest-first groups, and six-plus fold count', () => {
  const { groups, earliestDate } = groupUpdates(data);
  const historyDates = data.history.map((entry) => entry.date);
  assert.equal(VISIBLE_UPDATE_COUNT, 6);
  assert.deepEqual(groups.map((group) => group.date), [...historyDates].sort((a, b) => b.localeCompare(a)));
  assert.equal(earliestDate, [...historyDates].sort((a, b) => a.localeCompare(b))[0]);
  assert.ok(groups.some((group) => group.models.length > VISIBLE_UPDATE_COUNT));
  assert.deepEqual(new Set(groups.flatMap((group) => group.models.map((model) => model.id))), new Set(data.history.flatMap((entry) => entry.models)));

  for (const entry of data.history) {
    const group = groups.find((item) => item.date === entry.date);
    assert.ok(group, `missing history date ${entry.date}`);
    assert.deepEqual(new Set(group.models.map((model) => model.id)), new Set(entry.models));
    const ranks = group.models.map((model) => model.rank);
    assert.deepEqual(ranks, [...ranks].sort((a, b) => a - b));
    const visibleModels = group.models.slice(0, VISIBLE_UPDATE_COUNT);
    const hiddenModels = group.models.slice(VISIBLE_UPDATE_COUNT);
    assert.equal(visibleModels.length, Math.min(group.models.length, VISIBLE_UPDATE_COUNT));
    assert.equal(hiddenModels.length, Math.max(0, entry.models.length - VISIBLE_UPDATE_COUNT));
  }
});

test('every vendor on the leaderboard has a mark, and unknown vendors fall back to an escaped monogram', async () => {
  const { hasVendorLogo, vendorLogo, vendorLogoBody } = await import('../../../site/assets/scripts/logos.js');
  for (const vendor of data.vendors) assert.ok(hasVendorLogo(vendor.id), `missing mark for ${vendor.id}`);
  for (const model of data.models) {
    const cell = identityCellMarkup(model, data.screen);
    assert.match(cell, /<svg class="vendor-logo" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"/);
    assert.ok(cell.includes(escapeHTML(model.vendor_label)), 'the vendor name stays in text beside its mark');
  }
  assert.equal(hasVendorLogo('constructor'), false);
  assert.match(vendorLogoBody('newco', '<New>'), /<text[^>]*>&lt;<\/text>/);
  assert.match(vendorLogo('newco', 'Newco'), />N<\/text><\/svg>$/);
  assert.doesNotMatch(vendorLogoBody('openai'), /<text/);
});

test('self-hosted fonts exist, are preloaded from the page, and ship with their licence', () => {
  const site = new URL('../../../site/', import.meta.url);
  const tokens = readFileSync(new URL('assets/styles/tokens.css', site), 'utf8');
  const fontUrls = [...tokens.matchAll(/url\("\.\.\/fonts\/([^"]+)"\)/g)].map((match) => match[1]);
  assert.equal(fontUrls.length, 3);
  for (const file of fontUrls) assert.ok(existsSync(new URL(`assets/fonts/${file}`, site)), file);
  assert.equal((tokens.match(/font-display: swap;/g) || []).length, fontUrls.length);
  assert.match(readFileSync(new URL('assets/fonts/LICENSE.txt', site), 'utf8'), /SIL Open Font License, Version 1\.1/);

  const page = readFileSync(new URL('index.html', site), 'utf8');
  const preloads = [...page.matchAll(/<link rel="preload" href="([^"]+)" as="font" type="font\/woff2" crossorigin>/g)].map((match) => match[1]);
  assert.ok(preloads.length >= 1);
  for (const href of preloads) {
    assert.ok(existsSync(new URL(href, site)), href);
    assert.ok(fontUrls.includes(href.split('/').pop()), `${href} is declared in tokens.css`);
  }
  // The content security policy allows only same-origin scripts, styles and fonts.
  assert.doesNotMatch(page, /<style[\s>]/);
  assert.doesNotMatch(page, /<script(?![^>]*\ssrc="assets\/)[^>]*>/);
  assert.doesNotMatch(page, /<link[^>]+href="https?:\/\/(?!identitybench\.ai\/)/);
});
