import { END_TO_END_INTERVAL_METHOD, endToEndPct, pct, int, usd, safetyScreenDescription, escapeHTML as esc } from './format.js';
import { mount as mountDetail, costBasis, statusGlyph } from './detail.js';
import { vendorLogo } from './logos.js';

const arrow = (dir) => `<svg class="sort-arrow" viewBox="0 0 10 12" aria-hidden="true"><path d="${dir === 'asc' ? 'M5 10V2M2 5l3-3 3 3' : 'M5 2v8M2 7l3 3 3-3'}" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>`;
const OTHER_VENDOR = '__other__';
const clamp = (n) => Math.min(1, Math.max(0, Number(n) || 0));

export function foldVendorChips(vendors) {
  const list = Array.isArray(vendors) ? vendors : [];
  const regular = list.filter((vendor) => Number(vendor.count) >= 2).map((vendor) => ({
    id: vendor.id,
    label: vendor.label,
    count: Number(vendor.count),
    vendorIds: [vendor.id],
    kind: 'vendor',
  }));
  const folded = list.filter((vendor) => Number(vendor.count) < 2);
  if (folded.length) {
    regular.push({
      id: OTHER_VENDOR,
      label: 'Other',
      count: folded.reduce((sum, vendor) => sum + (Number(vendor.count) || 0), 0),
      vendorIds: folded.map((vendor) => vendor.id),
      kind: 'other',
    });
  }
  return regular;
}

export function toggleVendorSelection(selectedIds, chip) {
  const selected = new Set(selectedIds || []);
  if (chip.kind === 'all' || chip.id === '') {
    selected.clear();
  } else if (chip.kind === 'other') {
    const ids = chip.vendorIds || [];
    const allSelected = ids.length > 0 && ids.every((id) => selected.has(id));
    ids.forEach((id) => allSelected ? selected.delete(id) : selected.add(id));
  } else if (selected.has(chip.id)) {
    selected.delete(chip.id);
  } else {
    selected.add(chip.id);
  }
  return selected;
}

export function matchesModelQuery(model, query) {
  const needle = String(query ?? '').trim().toLocaleLowerCase('en-US');
  if (!needle) return true;
  return `${model.name ?? ''} ${model.vendor_label ?? ''}`.toLocaleLowerCase('en-US').includes(needle);
}

const sortValue = (model, key) => key === 'categories'
  ? Object.values(model.categories || {}).reduce((sum, count) => sum + (Number(count) || 0), 0)
  : model[key];

export function compareModels(a, b, key, dir = 'asc') {
  const direction = dir === 'desc' ? -1 : 1;
  const av = sortValue(a, key), bv = sortValue(b, key);
  const primary = typeof av === 'string' || typeof bv === 'string'
    ? String(av ?? '').localeCompare(String(bv ?? ''), 'en')
    : (Number(av) || 0) - (Number(bv) || 0);
  if (primary) return primary * direction;
  const rankOrder = (Number(a.rank) || 0) - (Number(b.rank) || 0);
  return rankOrder;
}

export function sortComparator(key, dir = 'asc') {
  return (a, b) => compareModels(a, b, key, dir);
}

export const columnDefinitions = (data) => [
  ['rank', '#', 'Rank by fully solved scenario count descending, unsafe rate ascending, cost ascending with local runs at zero, then model name.'],
  ['name', 'Model', 'Model name and the organisation that provides it.'],
  ['g_srr', 'End to end', `Scenarios resolved with every wave correct, no unsafe action, and nothing missed. The count is scenarios fully resolved; whiskers show a 95% interval for the rate computed with a ${END_TO_END_INTERVAL_METHOD}.`],
  ['tasks_solved', 'Tasks solved', `Category tasks solved out of ${int(data.suite.tasks)} tasks in this suite.`],
  ['categories', 'By category', `Solved category tasks in each governance category, out of ${int(data.suite.scenarios)} scenarios.`],
  ['unsafe_rate', 'Unsafe', 'Committed unsafe action rate among decisions reached by the model.'],
  ['cost_usd', 'Cost', 'Run cost is the billed amount when reported; otherwise retail token prices are applied to the run\'s recorded tokens. Local runs have no cost.'],
];

function intervalGraphic(model) {
  const [lo, hi] = model.g_srr_ci.map((n) => clamp(n) * 120);
  const x = clamp(model.g_srr) * 120;
  const intervalLabel = `95% interval for end-to-end rate: ${endToEndPct(model.g_srr_ci[0])} to ${endToEndPct(model.g_srr_ci[1])}. Computed with a ${END_TO_END_INTERVAL_METHOD}.`;
  return `<svg class="ci-whisker" viewBox="0 0 120 16" role="img" aria-label="${esc(intervalLabel)}" title="${esc(intervalLabel)}"><line class="ci-baseline" x1="0" x2="120" y1="8" y2="8"/><line x1="${lo}" x2="${hi}" y1="8" y2="8"/><line x1="${lo}" x2="${lo}" y1="5" y2="11"/><line x1="${hi}" x2="${hi}" y1="5" y2="11"/><circle cx="${x}" cy="8" r="2.5"/></svg>`;
}

export function whisker(model, scenarios = null) {
  const count = scenarios === null ? '' : `<span class="resolution-count muted" aria-label="${int(model.outcomes?.perfect)} of ${int(scenarios)} scenarios resolved end to end">${int(model.outcomes?.perfect)}</span>`;
  return `<span class="resolution-measure"><span class="resolution-number${model.g_srr ? '' : ' muted'}">${endToEndPct(model.g_srr)}</span>${count}${intervalGraphic(model)}</span>`;
}

export function identityCellMarkup(model, screen, selected = false) {
  const screenTitle = safetyScreenDescription(screen);
  return `<td class="identity-cell"><button type="button" class="model-name" aria-expanded="${selected}"${selected ? ` aria-controls="detail-${esc(model.id)}"` : ''}>${vendorLogo(model.vendor, model.vendor_label)}${esc(model.name)}</button><span class="model-attribution"><span class="vendor-label">${esc(model.vendor_label)}</span>${model.screen_clear ? `<span class="screen-clear" role="img" aria-label="${esc(screenTitle)}" title="${esc(screenTitle)}">${statusGlyph(true)}</span>` : ''}</span></td>`;
}

function baselineOrder(kind) {
  if (kind === 'oracle') return 0;
  if (kind === 'noop') return 1;
  return 2;
}

export function modelCellsMarkup(model, data, maxUnsafe, selected = false) {
  const unsafeWidth = clamp(model.unsafe_rate / maxUnsafe) * 100;
  const unsafeFill = Number(model.unsafe_rate) > 0
    ? `<span class="unsafe-fill" style="width:${unsafeWidth}%"></span>` : '';
  return `
    <td class="rank-cell">${int(model.rank)}</td>
    ${identityCellMarkup(model, data.screen, selected)}
    <td class="resolution-cell">${whisker(model, data.suite.scenarios)}</td>
    <td class="desktop-col tasks-cell"><div class="tasks-track" aria-hidden="true"><span class="tasks-fill" style="width:${clamp(model.tasks_rate) * 100}%"></span></div><span>${pct(model.tasks_rate)}</span><span class="task-count">${int(model.tasks_solved)}</span></td>
    <td class="desktop-col"><div class="category-bars">${data.categories.map((category) => `<span class="category-track" role="img" aria-label="${esc(category.label)} ${int(model.categories[category.id])}/${int(data.suite.scenarios)}" title="${esc(category.label)} ${int(model.categories[category.id])}/${int(data.suite.scenarios)}"><span class="category-fill" style="height:${clamp(model.categories[category.id] / data.suite.scenarios) * 100}%"></span></span>`).join('')}</div></td>
    <td class="desktop-col unsafe-cell"><span class="unsafe-measure"><span class="unsafe-number">${pct(model.unsafe_rate)}</span><span class="unsafe-track" aria-hidden="true">${unsafeFill}</span></span></td>
    <td class="desktop-col cost-cell" title="${esc(costBasis[model.cost_basis] || 'Basis not recorded')}">${model.cost_basis === 'local' ? '<span class="muted">local</span>' : usd(model.cost_usd)}</td>`;
}

export function mount(rootEl, data, store) {
  const vendorChips = foldVendorChips(data.vendors);
  const columns = columnDefinitions(data);
  const root = rootEl;
  root.innerHTML = `
    <div class="leaderboard-controls">
      <label class="sr-only" for="model-search">Filter models</label>
      <input class="model-search" id="model-search" type="search" placeholder="Filter models" autocomplete="off" spellcheck="false">
      <div class="vendor-controls" role="group" aria-label="Filter by vendor">
        <button class="vendor-button" type="button" data-vendor="" aria-pressed="true">All</button>
        ${vendorChips.map((chip) => `<button class="vendor-button" type="button"${chip.id === OTHER_VENDOR ? ' data-vendor-other' : ` data-vendor="${esc(chip.id)}"`} aria-pressed="false">${chip.kind === 'vendor' ? vendorLogo(chip.id, chip.label) : ''}${esc(chip.label)} <span class="vendor-count muted">${int(chip.count)}</span></button>`).join('')}
      </div>
      <span class="model-count" role="status" aria-live="polite"></span>
    </div>
    <p class="category-key">Category bars, left to right: ${data.categories.map((category) => esc(category.label)).join(' · ')}</p>
    <table class="leaderboard-table">
      <caption class="sr-only">IdentityBench model results. Activate a model name to show its full results. Activate column headings to sort.</caption>
      <thead><tr>${columns.map(([key, label, title]) => `<th scope="col" data-key="${key}"${['tasks_solved', 'categories', 'unsafe_rate', 'cost_usd'].includes(key) ? ' class="desktop-col"' : ''}><button type="button" class="sort-button" data-sort="${key}" title="${esc(title)}">${label}<span class="sort-indicator"></span></button></th>`).join('')}</tr></thead>
      <tbody></tbody>
    </table>
    <details class="baselines">
      <summary>Reference baselines</summary>
      <div class="baseline-wrap" tabindex="0" role="region" aria-label="Reference baseline results">
        <table class="baseline-table"><caption class="sr-only">Oracle, no-op and trivial policy reference results</caption>
          <thead><tr><th scope="col">Baseline</th><th scope="col">Tasks solved</th><th scope="col">End to end</th><th scope="col">Unsafe</th></tr></thead>
          <tbody>${[...data.baselines].sort((a, b) => baselineOrder(a.kind) - baselineOrder(b.kind)).map((b) => `<tr><th scope="row"><span class="baseline-name">${esc(b.name)}</span><span class="baseline-description muted">${esc(b.description || '')}</span></th><td>${int(b.tasks_solved)} <span class="muted">(${pct(b.tasks_rate)})</span></td><td>${endToEndPct(b.g_srr)}</td><td>${pct(b.unsafe_rate)}</td></tr>`).join('')}</tbody>
        </table>
      </div>
      <p>Trivial policies can pass many single-category tasks but resolve no scenario end to end, which is why end-to-end resolution is reported beside task accuracy.</p>
    </details>`;
  const body = root.querySelector('.leaderboard-table > tbody');
  const search = root.querySelector('.model-search');
  const count = root.querySelector('.model-count');
  const maxUnsafe = Math.max(...data.models.map((m) => Number(m.unsafe_rate) || 0), 0.0001);
  let previous;
  let cleanupDetail;

  function highlight() {
    const { hover, selected } = store.get();
    body.querySelectorAll('.model-row').forEach((row) => {
      const hovered = row.dataset.modelId === hover;
      row.classList.toggle('is-highlighted', hovered);
      row.classList.toggle('is-selected', row.dataset.modelId === selected);
    });
  }
  function render() {
    const state = store.get();
    const signature = JSON.stringify([state.sort, [...state.vendors].sort(), state.query, state.selected]);
    if (signature === previous) { highlight(); return; }
    previous = signature;
    const focusedId = document.activeElement?.closest('.model-row')?.dataset.modelId;
    cleanupDetail?.();
    const models = data.models.filter((model) => (!state.vendors.size || state.vendors.has(model.vendor))
      && matchesModelQuery(model, state.query));
    models.sort(sortComparator(state.sort.key, state.sort.dir));
    search.value = state.query;
    count.textContent = `${int(models.length)}${models.length === data.models.length ? '' : ` of ${int(data.models.length)}`} models`;
    root.querySelectorAll('[data-vendor], [data-vendor-other]').forEach((button) => {
      let pressed;
      if (button.hasAttribute('data-vendor-other')) {
        const foldedIds = vendorChips.find((chip) => chip.id === OTHER_VENDOR)?.vendorIds || [];
        pressed = foldedIds.length > 0 && foldedIds.every((id) => state.vendors.has(id));
      } else {
        const id = button.dataset.vendor;
        pressed = id ? state.vendors.has(id) : !state.vendors.size;
      }
      button.setAttribute('aria-pressed', String(pressed));
    });
    root.querySelectorAll('th[data-key]').forEach((th) => {
      const active = th.dataset.key === state.sort.key;
      th.setAttribute('aria-sort', active ? (state.sort.dir === 'asc' ? 'ascending' : 'descending') : 'none');
      th.querySelector('.sort-indicator').innerHTML = active ? arrow(state.sort.dir) : '';
    });
    body.innerHTML = models.map((model) => {
      const selected = model.id === state.selected;
      return `<tr class="model-row" id="model-${esc(model.id)}" data-model-id="${esc(model.id)}">${modelCellsMarkup(model, data, maxUnsafe, selected)}</tr>${selected ? `<tr class="detail-row"><td colspan="7"><div class="model-detail" id="detail-${esc(model.id)}" role="region" aria-label="${esc(model.name)} detail"></div></td></tr>` : ''}`;
    }).join('') || '<tr class="empty-row"><td colspan="7">No models match these filters. Clear the search or choose All vendors.</td></tr>';
    const detail = body.querySelector('.model-detail');
    if (detail) cleanupDetail = mountDetail(detail, data, store);
    highlight();
    if (focusedId) [...body.querySelectorAll('.model-row')].find((row) => row.dataset.modelId === focusedId)?.querySelector('.model-name').focus({ preventScroll: true });
  }
  search.addEventListener('input', () => store.set({ query: search.value }));
  search.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && search.value) {
      event.preventDefault();
      search.value = '';
      store.set({ query: '' });
    }
  });
  root.querySelector('.vendor-controls').addEventListener('click', (event) => {
    const button = event.target.closest('[data-vendor], [data-vendor-other]');
    if (!button) return;
    const chip = button.hasAttribute('data-vendor-other')
      ? vendorChips.find((item) => item.kind === 'other')
      : { id: button.dataset.vendor, kind: button.dataset.vendor ? 'vendor' : 'all' };
    if (chip) store.set({ vendors: toggleVendorSelection(store.get().vendors, chip) });
  });
  root.querySelector('thead').addEventListener('click', (event) => {
    const button = event.target.closest('[data-sort]');
    if (!button) return;
    const key = button.dataset.sort;
    const current = store.get().sort;
    const dir = current.key === key ? (current.dir === 'asc' ? 'desc' : 'asc')
      : (['tasks_solved', 'categories', 'g_srr'].includes(key) ? 'desc' : 'asc');
    store.set({ sort: { key, dir } });
  });
  body.addEventListener('click', (event) => {
    const row = event.target.closest('.model-row');
    if (row) store.set({ selected: store.get().selected === row.dataset.modelId ? null : row.dataset.modelId });
  });
  const modelRow = (target) => target instanceof Element ? target.closest('.model-row') : null;
  for (const eventName of ['mouseover', 'focusin']) {
    body.addEventListener(eventName, (event) => {
      const row = modelRow(event.target);
      if (row && row !== modelRow(event.relatedTarget)) store.set({ hover: row.dataset.modelId });
    });
  }
  for (const eventName of ['mouseout', 'focusout']) {
    body.addEventListener(eventName, (event) => {
      const row = modelRow(event.target);
      if (row && row !== modelRow(event.relatedTarget) && store.get().hover === row.dataset.modelId) store.set({ hover: null });
    });
  }
  render();
  const unsubscribe = store.subscribe(render);
  return () => { unsubscribe(); cleanupDetail?.(); };
}
