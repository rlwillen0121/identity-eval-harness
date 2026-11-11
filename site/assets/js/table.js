import { pct, int, usd, escapeHTML as esc } from './format.js';
import { mount as mountDetail, costBasis, statusGlyph } from './detail.js';

const arrow = (dir) => `<svg class="sort-arrow" viewBox="0 0 10 12" aria-hidden="true"><path d="${dir === 'asc' ? 'M5 10V2M2 5l3-3 3 3' : 'M5 2v8M2 7l3 3 3-3'}" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>`;
const columns = [
  ['rank', '#', 'Rank by tasks solved, then end-to-end resolution, unsafe rate and model name.'],
  ['name', 'Model', 'Model name and the organisation that provides it.'],
  ['tasks_solved', 'Tasks solved', 'Solved category tasks across all scenarios, shown as a share of the full suite.'],
  ['categories', 'By category', 'Solved tasks within each of the six governance categories.'],
  ['g_srr', 'End to end', 'Scenarios fully resolved across all three waves, with a 95% confidence interval.'],
  ['unsafe_rate', 'Unsafe', 'Share of the decisions a model reached where it committed an unsafe action.'],
  ['cost_usd', 'Cost', 'Cost of a complete run, using the recorded billing basis.'],
];
const clamp = (n) => Math.min(1, Math.max(0, n || 0));
function whisker(model) {
  const [lo, hi] = model.g_srr_ci.map((n) => clamp(n) * 120);
  const x = clamp(model.g_srr) * 120;
  const rate = pct(model.g_srr, model.g_srr ? 1 : 0);
  return `<span class="resolution-measure"><span class="resolution-number${model.g_srr ? '' : ' muted'}">${rate}</span><svg class="ci-whisker" viewBox="0 0 120 16" role="img" aria-label="95% interval: ${pct(model.g_srr_ci[0])} to ${pct(model.g_srr_ci[1])}"><line class="ci-baseline" x1="0" x2="120" y1="8" y2="8"/><line x1="${lo}" x2="${hi}" y1="8" y2="8"/><line x1="${lo}" x2="${lo}" y1="5" y2="11"/><line x1="${hi}" x2="${hi}" y1="5" y2="11"/><circle cx="${x}" cy="8" r="2.5"/></svg></span>`;
}

export function mount(rootEl, data, store) {
  const vendors = [...new Map(data.models.map((m) => [m.vendor, m.vendor_label])).entries()]
    .sort((a, b) => a[1].localeCompare(b[1], 'en'));
  rootEl.innerHTML = `
    <div class="leaderboard-controls">
      <label class="sr-only" for="model-search">Filter models</label>
      <input class="model-search" id="model-search" type="search" placeholder="Filter models" autocomplete="off">
      <div class="vendor-controls" role="group" aria-label="Filter by vendor">
        <button class="vendor-button" type="button" data-vendor="" aria-pressed="true">All</button>
        ${vendors.map(([id, label]) => `<button class="vendor-button" type="button" data-vendor="${esc(id)}" aria-pressed="false">${esc(label)}</button>`).join('')}
      </div>
      <span class="model-count" role="status" aria-live="polite"></span>
    </div>
    <p class="category-key">Category bars, left to right: ${data.categories.map((category) => esc(category.label)).join(' · ')}</p>
    <table class="leaderboard-table">
      <caption class="sr-only">IdentityBench model results. Activate a model name to show its full results. Activate column headings to sort.</caption>
      <thead><tr>${columns.map(([key, label, title], i) => `<th scope="col" data-key="${key}"${i > 2 ? ' class="desktop-col"' : ''}><button type="button" class="sort-button" data-sort="${key}" title="${title}">${label}<span class="sort-indicator"></span></button></th>`).join('')}</tr></thead>
      <tbody></tbody>
    </table>
    <details class="baselines">
      <summary>Reference baselines</summary>
      <div class="baseline-wrap" tabindex="0" role="region" aria-label="Reference baseline results">
        <table class="baseline-table"><caption class="sr-only">Oracle, no-op and trivial policy reference results</caption>
          <thead><tr><th scope="col">Baseline</th><th scope="col">Tasks solved</th><th scope="col">End to end</th><th scope="col">Unsafe</th></tr></thead>
          <tbody>${[...data.baselines].sort((a, b) => ({ oracle: 0, noop: 1, trivial: 2 })[a.kind] - ({ oracle: 0, noop: 1, trivial: 2 })[b.kind]).map((b) => `<tr><th scope="row">${esc(b.name)}</th><td>${int(b.tasks_solved)} <span class="muted">(${pct(b.tasks_rate)})</span></td><td>${pct(b.g_srr, b.g_srr ? 1 : 0)}</td><td>${pct(b.unsafe_rate)}</td></tr>`).join('')}</tbody>
        </table>
      </div>
      <p>Trivial policies can pass many single-category tasks but resolve no scenario end to end, which is why end-to-end resolution is reported beside task accuracy.</p>
    </details>`;
  const body = rootEl.querySelector('.leaderboard-table > tbody');
  const search = rootEl.querySelector('.model-search');
  const count = rootEl.querySelector('.model-count');
  const maxUnsafe = Math.max(...data.models.map((m) => m.unsafe_rate), 0.0001);
  let previous;
  let cleanupDetail;

  function highlight() {
    const { hover, selected } = store.get();
    body.querySelectorAll('.model-row').forEach((row) => {
      const hovered = row.dataset.modelId === hover;
      row.classList.toggle('is-highlighted', hovered);
      row.classList.toggle('is-receded', Boolean(hover) && !hovered && row.dataset.modelId !== selected);
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
    const query = state.query.trim().toLocaleLowerCase('en-US');
    const models = data.models.filter((m) => (!state.vendors.size || state.vendors.has(m.vendor))
      && `${m.name} ${m.vendor_label} ${m.id}`.toLocaleLowerCase('en-US').includes(query));
    const value = (m) => state.sort.key === 'categories'
      ? Object.values(m.categories).reduce((sum, n) => sum + n, 0) : m[state.sort.key];
    models.sort((a, b) => {
      const av = value(a), bv = value(b);
      const result = typeof av === 'string' ? av.localeCompare(bv, 'en') : (av ?? 0) - (bv ?? 0);
      return (state.sort.dir === 'desc' ? -result : result) || a.rank - b.rank;
    });
    search.value = state.query;
    count.textContent = `${int(models.length)}${models.length === data.models.length ? '' : ` of ${int(data.models.length)}`} models`;
    rootEl.querySelectorAll('[data-vendor]').forEach((button) => {
      button.setAttribute('aria-pressed', button.dataset.vendor ? state.vendors.has(button.dataset.vendor) : !state.vendors.size);
    });
    rootEl.querySelectorAll('th[data-key]').forEach((th) => {
      const active = th.dataset.key === state.sort.key;
      th.setAttribute('aria-sort', active ? (state.sort.dir === 'asc' ? 'ascending' : 'descending') : 'none');
      th.querySelector('.sort-indicator').innerHTML = active ? arrow(state.sort.dir) : '';
    });
    body.innerHTML = models.map((m) => {
      const selected = m.id === state.selected;
      return `<tr class="model-row" id="model-${esc(m.id)}" data-model-id="${esc(m.id)}">
        <td class="rank-cell">${int(m.rank)}</td>
        <td><button type="button" class="model-name" aria-expanded="${selected}"${selected ? ` aria-controls="detail-${esc(m.id)}"` : ''}>${esc(m.name)}</button><span class="vendor-label">${esc(m.vendor_label)}</span>${m.screen_clear ? `<span class="screen-clear" role="img" aria-label="Clears safety screen" title="Clears safety screen">${statusGlyph(true)}</span>` : ''}</td>
        <td class="tasks-cell"><div class="tasks-track" aria-hidden="true"><span class="tasks-fill" style="width:${clamp(m.tasks_rate) * 100}%"></span></div><span>${pct(m.tasks_rate)}</span><span class="task-count">${int(m.tasks_solved)}</span></td>
        <td class="desktop-col"><div class="category-bars">${data.categories.map((c) => `<span class="category-track" role="img" aria-label="${esc(c.label)} ${int(m.categories[c.id])}/${int(data.suite.scenarios)}" title="${esc(c.label)} ${int(m.categories[c.id])}/${int(data.suite.scenarios)}"><span class="category-fill" style="height:${clamp(m.categories[c.id] / data.suite.scenarios) * 100}%"></span></span>`).join('')}</div></td>
        <td class="desktop-col resolution-cell">${whisker(m)}</td>
        <td class="desktop-col unsafe-cell"><span class="unsafe-measure"><span class="unsafe-number">${pct(m.unsafe_rate)}</span><span class="unsafe-track" aria-hidden="true"><span class="unsafe-fill" style="width:${clamp(m.unsafe_rate / maxUnsafe) * 100}%"></span></span></span></td>
        <td class="desktop-col cost-cell" title="${esc(costBasis[m.cost_basis] || 'Basis not recorded')}">${m.cost_basis === 'local' ? '<span class="muted">local</span>' : usd(m.cost_usd)}</td>
      </tr>${selected ? `<tr class="detail-row"><td colspan="7"><div class="model-detail" id="detail-${esc(m.id)}" role="region" aria-label="${esc(m.name)} detail"></div></td></tr>` : ''}`;
    }).join('') || '<tr class="empty-row"><td colspan="7">No models match these filters. Clear the search or choose All vendors.</td></tr>';
    const detail = body.querySelector('.model-detail');
    if (detail) cleanupDetail = mountDetail(detail, data, store);
    highlight();
    if (focusedId) [...body.querySelectorAll('.model-row')].find((row) => row.dataset.modelId === focusedId)?.querySelector('.model-name').focus({ preventScroll: true });
  }
  search.addEventListener('input', () => store.set({ query: search.value }));
  rootEl.querySelector('.vendor-controls').addEventListener('click', (event) => {
    const button = event.target.closest('[data-vendor]');
    if (!button) return;
    const vendors = new Set(store.get().vendors);
    const id = button.dataset.vendor;
    if (!id) vendors.clear();
    else if (vendors.has(id)) vendors.delete(id);
    else vendors.add(id);
    store.set({ vendors });
  });
  rootEl.querySelector('thead').addEventListener('click', (event) => {
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
