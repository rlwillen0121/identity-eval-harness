import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { columnDefinitions, compareModels, foldVendorChips, identityCellMarkup, matchesModelQuery, modelCellsMarkup, mount, pendingMarkup, referenceMarkup, sortComparator, toggleVendorSelection, unsafeSummary, whisker } from '../assets/scripts/table.js';
import { fieldMedians, OUTCOME_SEGMENTS } from '../assets/scripts/detail.js';
import { heroFigures, leaderboardLede, provenanceRows } from '../assets/scripts/main.js';
import { groupUpdates, mount as mountUpdates, VISIBLE_UPDATE_COUNT } from '../assets/scripts/updates.js';
import { END_TO_END_INTERVAL_METHOD, escapeHTML, int, tokens } from '../assets/scripts/format.js';
import { buildLeaderboardData, isRankable, rankComparator, serializeLeaderboardData, wilsonInterval } from '../build.mjs';
import { makeStore } from './dom-stub.mjs';

const data = JSON.parse(readFileSync(new URL('../data/leaderboard.json', import.meta.url), 'utf8'));
const runsDir = new URL('../../reports/v51/runs/', import.meta.url);
const sortableColumns = ['rank', 'name', 'g_srr', 'state_accuracy', 'categories', 'unsafe_slot_rate', 'output_tokens', 'cost_usd'];
const round4 = (n) => Math.round(n * 10000) / 10000;
// Retired-generation references that must not appear in rendered copy. "v5.1" and "v51_*" are allowed.
const RETIRED = [/\bv5(?![.\d])/, /\bv5_core\b/, /(?<![\d,.])144(?![\d,]| ?k)/, /\btrials\b/, /Sem-SRR|G-PROP|Action-SRR/];

function median(values) {
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

function assertNoRetiredCopy(text, where) {
  for (const pattern of RETIRED) assert.doesNotMatch(text, pattern, `${where} contains retired reference ${pattern}`);
}

function stubTableRoot() {
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
  const root = { innerHTML: '', querySelector: (selector) => elements[selector] || null, querySelectorAll: () => [] };
  return { root, body, detail };
}

test('leaderboard.json follows the v5.1 contract and matches build.mjs byte for byte', () => {
  assert.equal(data.schema, 3);
  assert.equal(data.benchmark, 'IdentityBench v5.1');
  assert.equal(data.suite.id, 'v51_core');
  assert.equal(data.suite.spec_version, 'SPEC-v5.1');
  assert.equal(data.suite.episodes, 48);
  assert.equal(data.suite.repeats, 1);
  assert.equal(data.suite.slots_per_run, 48);
  assert.equal(data.suite.turn_budget, 24);
  assert.equal(data.suite.categories, 8);
  assert.equal(data.suite.families, 12);
  assert.equal(data.suite.regimes, 4);
  assert.equal(data.suite.contract_id, 'identitybench.v5.1.practitioner-contract.v2');
  for (const key of ['seal_sha256', 'episodes_sha256', 'oracles_sha256', 'spec_sha256']) assert.match(data.suite[key], /^[0-9a-f]{64}$/);
  assert.equal(data.categories.length, 8);
  assert.equal(data.families.length, 12);
  assert.deepEqual(data.subscores.map((s) => s.id), ['nhi_custody', 'agent_delegation', 'iiq_to_isc']);
  assert.equal(data.baselines, undefined, 'retired trivial baselines are gone');
  assert.equal(data.reference.gate, 'G-GOLD-REPLAY-V51');
  assert.equal(data.reference.g_srr_slots, 48);
  assert.ok(data.models.length >= 1);
  for (const model of data.models) {
    assert.equal(model.is_complete, true);
    assert.equal(model.observed_slots, 48);
    assert.equal(model.planned_slots, 48);
    assert.equal(model.outcomes.provider_error, 0);
    assert.equal(Object.values(model.outcomes).reduce((a, b) => a + b, 0), 48, `${model.id} outcomes cover every slot`);
    assert.equal(model.unsafe_count, model.overgrant_count + model.avail_breach_count);
    assert.ok(model.unsafe_slots <= model.planned_slots);
    assert.equal(model.unsafe_slot_rate, round4(model.unsafe_slots / model.planned_slots));
    assert.equal(model.g_srr, round4(model.g_srr_slots / model.planned_slots));
    assert.equal(model.cost_basis, 'retail_api_estimate');
    for (const retired of ['sem_srr_commit', 'g_prop', 'unsafe_targets', 'unsafe_rate', 'execution_valid_rate']) {
      assert.equal(model[retired], undefined, `${model.id} still has ${retired}`);
    }
    const familyPasses = Object.values(model.families).reduce((sum, fam) => sum + fam.pass_count, 0);
    assert.equal(familyPasses, model.g_srr_slots, `${model.id} family passes add up to end-to-end passes`);
  }
  const rebuilt = serializeLeaderboardData(buildLeaderboardData());
  const committed = readFileSync(new URL('../data/leaderboard.json', import.meta.url), 'utf8');
  assert.equal(committed, rebuilt);
});

test('ranking follows G-SRR, then state accuracy, then fewer violations, then lower cost, then name', () => {
  assert.deepEqual(data.models.map((m) => m.rank), data.models.map((_, i) => i + 1));
  for (let i = 1; i < data.models.length; i++) {
    const [a, b] = [data.models[i - 1], data.models[i]];
    assert.ok(a.g_srr >= b.g_srr, `${a.id} before ${b.id} by G-SRR`);
    if (a.g_srr === b.g_srr) assert.ok(a.state_accuracy >= b.state_accuracy, `${a.id} before ${b.id} by state accuracy`);
  }
  const base = { g_srr: 0.5, state_accuracy: 0.9, unsafe_count: 3, cost_sort: 10, name: 'M' };
  const order = (items) => [...items].sort(rankComparator).map((m) => m.id);
  assert.deepEqual(order([{ ...base, id: 'low', g_srr: 0.4 }, { ...base, id: 'high', g_srr: 0.6 }]), ['high', 'low']);
  assert.deepEqual(order([{ ...base, id: 'b', state_accuracy: 0.8 }, { ...base, id: 'a' }]), ['a', 'b']);
  assert.deepEqual(order([{ ...base, id: 'unsafe', unsafe_count: 9 }, { ...base, id: 'safe', unsafe_count: 0 }]), ['safe', 'unsafe']);
  assert.deepEqual(order([{ ...base, id: 'dear', cost_sort: 50 }, { ...base, id: 'cheap', cost_sort: 5 }]), ['cheap', 'dear']);
  assert.deepEqual(order([{ ...base, id: 'z', name: 'Zed' }, { ...base, id: 'a', name: 'Alpha' }]), ['a', 'z']);
  // Earlier keys dominate later ones.
  assert.deepEqual(order([{ ...base, id: 'cheap-unsafe', g_srr: 0.6, unsafe_count: 99, cost_sort: 1 }, { ...base, id: 'top', g_srr: 0.7, unsafe_count: 0, cost_sort: 999 }]), ['top', 'cheap-unsafe']);
});

test('incomplete runs are pending only: never ranked, counted as vendors, or used in medians', () => {
  const summary = { spec_version: 'SPEC-v5.1', suite_id: 'v51_core', is_complete: true, planned_slots: 48, observed_slots: 48, provider_error_slots: 0, g_srr: 0.5 };
  assert.equal(isRankable(summary, 48), true);
  assert.equal(isRankable({ ...summary, is_complete: false }, 48), false);
  assert.equal(isRankable({ ...summary, observed_slots: 47 }, 48), false);
  assert.equal(isRankable({ ...summary, provider_error_slots: 1 }, 48), false);
  assert.equal(isRankable({ ...summary, suite_id: 'v51_dev' }, 48), false);
  assert.equal(isRankable({ ...summary, g_srr: null }, 48), false);
  assert.equal(isRankable({ ...summary, planned_slots: 24, observed_slots: 24 }, 48), false);

  const rankedIds = new Set(data.models.map((m) => m.id));
  const incomplete = readdirSync(runsDir)
    .map((dir) => JSON.parse(readFileSync(new URL(`${dir}/summary.json`, runsDir), 'utf8')))
    .filter((s) => s.suite_id === 'v51_core' && !(s.is_complete && s.observed_slots === s.planned_slots));
  assert.ok(incomplete.length > 0, 'fixture data includes in-progress runs');
  for (const s of incomplete) {
    if (rankedIds.has(s.model_label)) continue;
    const pending = data.pending.find((p) => p.id === s.model_label);
    assert.ok(pending, `${s.model_label} is listed as pending`);
    assert.equal(pending.observed_slots, s.observed_slots);
    assert.equal(pending.provider_error_slots, s.provider_error_slots);
    assert.deepEqual(pending.g_srr_bounds, s.provisional_g_srr_bounds.map(round4));
  }
  for (const pending of data.pending) {
    assert.ok(!rankedIds.has(pending.id), `${pending.id} must not be ranked`);
    assert.ok(pending.observed_slots < pending.planned_slots);
    assert.ok(pending.g_srr_bounds[0] <= pending.g_srr_bounds[1]);
  }
  assert.equal(data.vendors.reduce((sum, v) => sum + v.count, 0), data.models.length, 'vendor counts cover ranked models only');
  assert.ok(data.history.every((entry) => entry.models.every((id) => rankedIds.has(id))), 'history lists ranked models only');

  const markup = pendingMarkup(data);
  assert.match(markup, /In progress · not ranked/);
  for (const pending of data.pending) {
    assert.ok(markup.includes(escapeHTML(pending.name)));
    assert.ok(markup.includes(`${pending.observed_slots}/${pending.planned_slots}`));
  }
  assert.equal(pendingMarkup({ ...data, pending: [] }), '');
});

test('Wilson score interval matches closed forms and every model interval', () => {
  const z2 = 1.959963984540054 ** 2;
  const [lo0, hi0] = wilsonInterval(0, 48);
  assert.equal(lo0, 0);
  assert.ok(Math.abs(hi0 - z2 / (48 + z2)) < 1e-12, 'k = 0 upper bound is z²/(n + z²)');
  const [loN, hiN] = wilsonInterval(48, 48);
  assert.ok(Math.abs(loN - 48 / (48 + z2)) < 1e-12, 'k = n lower bound is n/(n + z²)');
  assert.equal(hiN, 1);
  for (let k = 0; k <= 48; k++) {
    const [lo, hi] = wilsonInterval(k, 48);
    const [mirrorLo, mirrorHi] = wilsonInterval(48 - k, 48);
    assert.ok(lo <= k / 48 && k / 48 <= hi, `interval contains p for k = ${k}`);
    assert.ok(Math.abs(lo - (1 - mirrorHi)) < 1e-12 && Math.abs(hi - (1 - mirrorLo)) < 1e-12, `symmetric for k = ${k}`);
  }
  // Textbook check: 42/48 gives roughly 75.3% to 94.1%.
  assert.deepEqual(wilsonInterval(42, 48).map(round4), [0.753, 0.9414]);
  assert.deepEqual(wilsonInterval(0, 0), [0, 1]);
  for (const model of data.models) {
    assert.deepEqual(model.g_srr_ci, wilsonInterval(model.g_srr_slots, model.planned_slots).map(round4), model.id);
  }
  assert.match(END_TO_END_INTERVAL_METHOD, /^Wilson score interval \(z = 1\.96\)/);
  assert.doesNotMatch(END_TO_END_INTERVAL_METHOD, /bootstrap/);
});

test('table columns and titles describe v5.1 metrics', () => {
  const columns = columnDefinitions(data);
  assert.deepEqual(columns.map(([key, label]) => [key, label]), [
    ['rank', '#'], ['name', 'Model'], ['g_srr', 'End to end'], ['state_accuracy', 'State accuracy'],
    ['categories', 'By category'], ['unsafe_slot_rate', 'Unsafe'], ['output_tokens', 'Total tokens'], ['cost_usd', 'Est. cost'],
  ]);
  const titles = new Map(columns.map(([key, , title]) => [key, title]));
  assert.match(titles.get('rank'), /^Rank by .*then model name\.$/);
  assert.ok(titles.get('g_srr').includes(END_TO_END_INTERVAL_METHOD));
  assert.match(titles.get('g_srr'), /out of 48 \(48 engagements × 1 run\)/);
  assert.match(titles.get('g_srr'), /24-turn cap with all 8 categories/);
  assert.match(titles.get('state_accuracy'), /assertions/);
  assert.match(titles.get('categories'), /8 IGA categories/);
  assert.match(titles.get('unsafe_slot_rate'), /overgrant or availability breach/);
  assert.match(titles.get('cost_usd'), /retail API rates/);
  assert.match(titles.get('cost_usd'), /subscription CLIs/);
});

test('model cells show interval, state accuracy, violation share and estimated cost', () => {
  const zeroWhisker = whisker({ g_srr: 0, g_srr_ci: [0, 0.0741], g_srr_slots: 0 }, 48);
  assert.match(zeroWhisker, /resolution-number muted">0%<\/span>/);
  assert.match(zeroWhisker, /aria-label="0 of 48 engagements resolved end to end">0<\/span>/);
  assert.match(zeroWhisker, /95% interval for end-to-end rate: 0% to 7\.4%/);

  const top = data.models[0];
  const cells = modelCellsMarkup(top, data);
  const markers = [
    '<td class="rank-cell">', '<td class="identity-cell"><button', '<td class="resolution-cell">',
    '<td class="desktop-col tasks-cell">', '<td class="desktop-col"><div class="category-bars">',
    '<td class="desktop-col unsafe-cell"', '<td class="desktop-col tokens-cell"', '<td class="desktop-col cost-cell"',
  ].map((marker) => cells.indexOf(marker));
  assert.ok(markers.every((position) => position >= 0));
  assert.deepEqual(markers, [...markers].sort((a, b) => a - b), 'tbody uses the 8-column order');
  assert.equal((cells.match(/class="category-track"/g) || []).length, 8);
  assert.ok(cells.includes(`>${tokens(top.output_tokens)}</td>`));
  assert.ok(cells.includes(escapeHTML(unsafeSummary(top, 48))));

  const unsafe = data.models.find((m) => m.unsafe_slots > 0);
  if (unsafe) {
    const unsafeCells = modelCellsMarkup(unsafe, data);
    assert.match(unsafeCells, /class="unsafe-fill"/);
    assert.match(unsafeSummary(unsafe, 48), new RegExp(`^${unsafe.unsafe_slots} engagements? of 48 with a committed violation`));
  }
  const clean = data.models.find((m) => m.unsafe_slots === 0);
  if (clean) assert.doesNotMatch(modelCellsMarkup(clean, data), /class="unsafe-fill"/);
  assert.equal(unsafeSummary({ unsafe_slots: 1, unsafe_count: 2, overgrant_count: 1, avail_breach_count: 1 }, 48),
    '1 engagement of 48 with a committed violation · 2 violations (1 overgrant, 1 availability breach)');

  const css = readFileSync(new URL('../assets/styles/table.css', import.meta.url), 'utf8');
  const mobile = css.split('@media (max-width: 759px)')[1];
  assert.match(mobile, /\.desktop-col \{ display: none; \}/);
  assert.match(mobile, /\.ci-whisker \{ display: none; \}/);
});

test('hero figures, lede and provenance are derived from the v5.1 suite', () => {
  assert.deepEqual(heroFigures(data, data.models[0]), [
    [String(data.models.length), 'ranked models'],
    ['48', 'slots (48 engagements × 1)'],
    ['12', 'config files authored per engagement'],
    ['87.5%', 'top end-to-end'],
  ]);
  assert.equal(
    leaderboardLede(data),
    'Ranked by how many of 48 client engagements each resolves end to end: every category correct, nothing unsafe, committed within the turn cap.',
  );
  const rows = new Map(provenanceRows(data));
  assert.equal(rows.get('Suite'), 'v51_core');
  assert.equal(rows.get('Specification'), 'SPEC-v5.1 · generator 5.1.0');
  assert.equal(rows.get('Seal SHA-256'), data.suite.seal_sha256);
  assert.equal(rows.get('Specification SHA-256'), data.suite.spec_sha256);
});

test('every sortable column sorts independently in both directions', () => {
  const fixture = [
    { id: 'low', rank: 3, name: 'Alpha', g_srr: 0.1, state_accuracy: 0.2, categories: { a: { pass_count: 1 } }, unsafe_slot_rate: 0.01, unsafe_count: 1, output_tokens: 1000, cost_usd: 1, cost_basis: 'retail_api_estimate' },
    { id: 'middle', rank: 1, name: 'Bravo', g_srr: 0.2, state_accuracy: 0.4, categories: { a: { pass_count: 2 } }, unsafe_slot_rate: 0.02, unsafe_count: 2, output_tokens: 2000, cost_usd: 2, cost_basis: 'retail_api_estimate' },
    { id: 'high', rank: 2, name: 'Charlie', g_srr: 0.3, state_accuracy: 0.6, categories: { a: { pass_count: 3 } }, unsafe_slot_rate: 0.03, unsafe_count: 3, output_tokens: 3000, cost_usd: 3, cost_basis: 'retail_api_estimate' },
  ];
  const originalIds = data.models.map((model) => model.id);
  for (const key of sortableColumns) {
    const ascending = key === 'rank' ? ['middle', 'high', 'low'] : ['low', 'middle', 'high'];
    const descending = key === 'rank' ? ['low', 'high', 'middle'] : ['high', 'middle', 'low'];
    assert.deepEqual([...fixture].sort(sortComparator(key, 'asc')).map((model) => model.id), ascending, `${key} ascending`);
    assert.deepEqual([...fixture].sort(sortComparator(key, 'desc')).map((model) => model.id), descending, `${key} descending`);
  }
  assert.deepEqual(data.models.map((model) => model.id), originalIds);
});

test('rank is the ascending tie-break, local cost sorts as zero, and unsafe ties break on violation totals', () => {
  const shared = { name: 'Equal', g_srr: 0.5, state_accuracy: 0.5, categories: { a: { pass_count: 20 } }, unsafe_slot_rate: 0.1, unsafe_count: 4, output_tokens: 5000, cost_usd: 10, cost_basis: 'retail_api_estimate' };
  for (const key of sortableColumns) {
    for (const direction of ['asc', 'desc']) {
      const first = { ...shared, id: 'rank-two', rank: 2 };
      const second = { ...shared, id: 'rank-one', rank: 1 };
      if (key === 'rank') first.rank = second.rank = 1;
      assert.ok(compareModels(second, first, key, direction) <= 0, `${key} ${direction} rank tie-break`);
    }
  }
  const local = { ...shared, id: 'local', rank: 2, cost_usd: 999, cost_basis: 'local' };
  const paid = { ...shared, id: 'paid', rank: 1, cost_usd: 1, cost_basis: 'retail_api_estimate' };
  assert.ok(compareModels(local, paid, 'cost_usd', 'asc') < 0);
  const fewer = { ...shared, id: 'fewer', rank: 2, unsafe_count: 3 };
  const more = { ...shared, id: 'more', rank: 1, unsafe_count: 3000 };
  assert.ok(compareModels(fewer, more, 'unsafe_slot_rate', 'asc') < 0);
});

test('vendor chips reconcile model counts and fold singleton vendors into Other', () => {
  const chips = foldVendorChips(data.vendors);
  const visible = data.vendors.filter((vendor) => vendor.count >= 2);
  const singletons = data.vendors.filter((vendor) => vendor.count < 2);
  assert.equal(chips.reduce((sum, chip) => sum + chip.count, 0), data.models.length);
  assert.deepEqual(chips.filter((chip) => chip.kind === 'vendor').map((chip) => chip.id), visible.map((vendor) => vendor.id));
  const other = chips.find((chip) => chip.kind === 'other');
  if (singletons.length) {
    assert.deepEqual(other.vendorIds, singletons.map((vendor) => vendor.id));
    const allFolded = toggleVendorSelection(new Set(), other);
    assert.deepEqual(allFolded, new Set(other.vendorIds));
    assert.deepEqual(toggleVendorSelection(allFolded, { id: '', kind: 'all' }), new Set());
  } else {
    assert.equal(other, undefined);
  }
});

test('model query matching is case-insensitive across name and vendor label', () => {
  const model = { id: 'private-model-id', name: 'Example Model', vendor_label: 'Sample Vendor' };
  assert.equal(matchesModelQuery(model, 'example'), true);
  assert.equal(matchesModelQuery(model, 'SAMPLE VENDOR'), true);
  assert.equal(matchesModelQuery(model, 'private-model-id'), false);
});

test('category, subscore and family medians use ranked models only', () => {
  const medians = fieldMedians(data);
  assert.deepEqual(Object.keys(medians.categories), data.categories.map((category) => category.id));
  assert.deepEqual(Object.keys(medians.families), data.families.map((family) => family.id));
  assert.deepEqual(Object.keys(medians.subscores), data.subscores.map((subscore) => subscore.id));
  for (const category of data.categories) {
    const rates = data.models.map((model) => model.categories[category.id].pass_count / model.categories[category.id].planned_slots);
    assert.equal(medians.categories[category.id], median(rates), `${category.id} median`);
  }
  for (const family of data.families) {
    const rates = data.models.map((model) => model.families[family.id].pass_count / model.families[family.id].slots);
    assert.equal(medians.families[family.id], median(rates), `${family.id} median`);
  }
  const withPendingAsModels = fieldMedians({ ...data, models: [...data.models, ...data.pending.map((p) => ({ ...p, categories: {} }))] });
  if (data.pending.length) assert.notDeepEqual(withPendingAsModels, medians, 'pending runs would change medians if they were mixed in');
});

test('updates group history by date, carry the launch note, and list ranked models', () => {
  const { groups, earliestDate } = groupUpdates(data);
  const historyDates = data.history.map((entry) => entry.date);
  assert.equal(VISIBLE_UPDATE_COUNT, 6);
  assert.deepEqual(groups.map((group) => group.date), [...new Set(historyDates)].sort((a, b) => b.localeCompare(a)));
  assert.equal(earliestDate, '2026-11-22');
  assert.deepEqual(new Set(groups.flatMap((group) => group.models.map((model) => model.id))), new Set(data.history.flatMap((entry) => entry.models)));
  const launch = groups.find((group) => group.date === '2026-11-22');
  assert.match(launch.notes[0], /v5\.1 launches/);
  assert.match(launch.notes[0], /retired to the repository archive/);
  const root = { innerHTML: '', addEventListener() {}, removeEventListener() {}, querySelectorAll: () => [] };
  mountUpdates(root, data, makeStore());
  assert.match(root.innerHTML, /<p class="updates-note">IdentityBench v5\.1 launches/);
  assert.match(root.innerHTML, /When each model's 48-engagement run was first published\./);
});

test('every vendor in the data has a mark, and unknown vendors fall back to an escaped monogram', async () => {
  const { hasVendorLogo, vendorLogo, vendorLogoBody } = await import('../assets/scripts/logos.js');
  const vendors = new Set([...data.vendors.map((v) => v.id), ...data.models.map((m) => m.vendor), ...data.pending.map((p) => p.vendor)]);
  for (const vendor of vendors) assert.ok(hasVendorLogo(vendor), `missing mark for ${vendor}`);
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

test('page shell: fonts, self-only CSP, v5.1 chip and meta, study disclaimers, and v5.1 sources', () => {
  const site = new URL('../', import.meta.url);
  const tokensCss = readFileSync(new URL('assets/styles/tokens.css', site), 'utf8');
  const fontUrls = [...tokensCss.matchAll(/url\("\.\.\/fonts\/([^"]+)"\)/g)].map((match) => match[1]);
  assert.equal(fontUrls.length, 3);
  for (const file of fontUrls) assert.ok(existsSync(new URL(`assets/fonts/${file}`, site)), file);

  const page = readFileSync(new URL('index.html', site), 'utf8');
  assert.match(page, /<span class="version">v5\.1 · core<\/span>/);
  assert.match(page, /<title>IdentityBench v5\.1 · /);
  for (const property of ['og:title', 'og:description', 'og:image:alt']) {
    assert.match(page, new RegExp(`<meta property="${property}" content="[^"]*IdentityBench v5\\.1`), property);
  }
  for (const name of ['twitter:title', 'twitter:description', 'description']) {
    assert.match(page, new RegExp(`<meta name="${name}" content="[^"]*IdentityBench v5\\.1`), name);
  }
  for (const source of ['SPEC-v5.1.md', 'datasets/README.md', 'datasets/v51/v51_core/SEAL.json', 'archive/README.md']) {
    assert.ok(page.includes(`/blob/main/${source}"`), `links ${source}`);
  }
  assert.doesNotMatch(page, /<style[\s>]/);
  assert.doesNotMatch(page, /<script(?![^>]*\ssrc="assets\/)[^>]*>/);
  assert.doesNotMatch(page, /<link[^>]+href="https?:\/\/(?!identitybench\.ai\/)/);
  assert.match(page, /<p class="study-note">[^<]*Independent exploratory research study[^<]*synthetic environments[^<]*not an official or vendor-endorsed benchmark\.?<\/p>/i);
  assert.match(page, /<footer class="site-footer">[\s\S]*independent, exploratory research study[\s\S]*synthetic enterprise environments[\s\S]*not an official standards-body or vendor-endorsed benchmark[\s\S]*<\/footer>/i);
  assertNoRetiredCopy(page, 'index.html');
  assertNoRetiredCopy(readFileSync(new URL('404.html', site), 'utf8'), '404.html');
});

test('mounted table renders pending and reference blocks, the v5.1 detail panel, and no retired copy', () => {
  const { root, body, detail } = stubTableRoot();
  const previousDocument = globalThis.document;
  globalThis.document = { activeElement: null };
  try {
    const store = makeStore({ selected: data.models[0].id });
    const unmount = mount(root, data, store);
    assert.match(root.innerHTML, /<summary>Show reference replay<\/summary>/);
    assert.ok(root.innerHTML.includes(referenceMarkup(data).trim()));
    assert.ok(root.innerHTML.includes('In progress · not ranked'));
    assert.match(root.innerHTML, /<span class="category-key">By category: Identity data · Lifecycle · NHI &amp; agents/);
    assert.match(root.innerHTML, /<th scope="col" data-key="unsafe_slot_rate" class="desktop-col">/);
    for (const pending of data.pending) {
      assert.ok(!body.innerHTML.includes(`data-model-id="${escapeHTML(pending.id)}"`), `${pending.id} is not a ranked row`);
    }
    assert.equal((body.innerHTML.match(/class="model-row"/g) || []).length, data.models.length);
    assert.match(body.innerHTML, /<tr class="detail-row"><td colspan="8"><div class="model-detail"/);
    for (const heading of ['IGA categories', 'Subscores', 'Resolution &amp; safety', 'Engagement outcomes', 'Engagement families (F01–F12)']) {
      assert.ok(detail.innerHTML.includes(heading), heading);
    }
    for (const category of data.categories) assert.ok(detail.innerHTML.includes(escapeHTML(category.title)), category.title);
    for (const [, label] of OUTCOME_SEGMENTS) assert.ok(detail.innerHTML.includes(label), label);
    for (const label of ['Turn-limit stops', 'Parse failures', 'Provider errors', 'Estimated cost', 'NHI custody', 'Agent delegation']) {
      assert.ok(detail.innerHTML.includes(label), label);
    }
    assert.ok(detail.innerHTML.includes(`${tokens(data.models[0].output_tokens)} (${int(data.models[0].output_tokens)} total`));
    for (const text of [root.innerHTML, body.innerHTML, detail.innerHTML]) assertNoRetiredCopy(text, 'table');

    store.set({ selected: null, query: '__no_such_model__' });
    assert.equal(body.innerHTML, '<tr class="empty-row"><td colspan="8">No models match these filters. Clear the search or choose All vendors.</td></tr>');
    unmount();
  } finally {
    globalThis.document = previousDocument;
  }
});
