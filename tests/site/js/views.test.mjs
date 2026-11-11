import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { columnDefinitions, compareModels, foldVendorChips, identityCellMarkup, matchesModelQuery, sortComparator, toggleVendorSelection, whisker } from '../../../site/assets/js/table.js';
import { fieldMedians } from '../../../site/assets/js/detail.js';
import { heroSentence } from '../../../site/assets/js/main.js';
import { groupUpdates, VISIBLE_UPDATE_COUNT } from '../../../site/assets/js/updates.js';
import { END_TO_END_INTERVAL_METHOD, endToEndPct, escapeHTML, pct } from '../../../site/assets/js/format.js';

const data = JSON.parse(readFileSync(new URL('../../../site/data/leaderboard.json', import.meta.url), 'utf8'));
const sortableColumns = ['rank', 'name', 'tasks_solved', 'categories', 'g_srr', 'unsafe_rate', 'cost_usd'];

function median(values) {
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

function heroExpected(models) {
  const top = [...models].sort((a, b) => a.rank - b.rank)[0];
  const middleRate = median(models.map((model) => model.g_srr));
  const summary = middleRate === 0
    ? `${models.filter((model) => model.g_srr === 0).length} of ${models.length} models resolve none.`
    : `The median model resolves ${endToEndPct(middleRate)}.`;
  return `${top.name} resolves ${endToEndPct(top.g_srr)} of enterprise identity scenarios end to end. ${summary}`;
}

test('table interval and cost titles match the published methods', () => {
  const titles = new Map(columnDefinitions(data).map(([key, , title]) => [key, title]));
  assert.ok(titles.get('g_srr').includes(END_TO_END_INTERVAL_METHOD));
  assert.match(titles.get('g_srr'), /95% interval/);
  assert.match(titles.get('cost_usd'), /billed amount when reported/);
  assert.match(titles.get('cost_usd'), /retail token prices/);
  assert.match(titles.get('cost_usd'), /Local runs have no cost/);
});

test('zero end-to-end whiskers and mobile identity rows use consistent method and layout labels', () => {
  const zeroWhisker = whisker({ g_srr: 0, g_srr_ci: [0, 0] });
  assert.match(zeroWhisker, /resolution-number muted">0%<\/span>/);
  assert.match(zeroWhisker, /95% interval for end-to-end rate.*0% to 0%/);
  assert.doesNotMatch(zeroWhisker, /0\.0%/);

  const passingModel = data.models.find((model) => model.screen_clear);
  const identityCell = identityCellMarkup(passingModel, data.screen);
  assert.ok(identityCell.indexOf('class="model-name"') < identityCell.indexOf('class="model-attribution"'));
  for (const check of data.screen) assert.ok(identityCell.includes(escapeHTML(check.label)), check.label);

  const css = readFileSync(new URL('../../../site/assets/css/table.css', import.meta.url), 'utf8');
  const mobile = css.split('@media (max-width: 759px)')[1];
  assert.match(css, /\.model-attribution \{ display: contents; \}/);
  assert.match(mobile, /\.model-name \{ display: block;/);
  assert.match(mobile, /\.model-attribution \{ display: block; \}/);
  assert.match(mobile, /\.vendor-label \{ display: inline; margin-left: 0; \}/);
  assert.match(css, /\.vendor-label \{ display: block; margin-left: 0; \}/);
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

test('hero sentence handles positive and zero suite medians', () => {
  assert.equal(heroSentence(data), heroExpected(data.models));
  const positive = [
    { rank: 2, name: 'Second', g_srr: 0.25 },
    { rank: 1, name: 'Top', g_srr: 1 },
  ];
  const zeroMedian = [
    { rank: 1, name: 'Top', g_srr: 0.25 },
    { rank: 2, name: 'Second', g_srr: 0 },
    { rank: 3, name: 'Third', g_srr: 0 },
  ];
  assert.equal(heroSentence({ models: positive }), heroExpected(positive));
  assert.equal(heroSentence({ models: zeroMedian }), heroExpected(zeroMedian));
  assert.match(heroSentence({ models: [{ rank: 1, name: 'Zero', g_srr: 0 }] }), /Zero resolves 0%/);
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
