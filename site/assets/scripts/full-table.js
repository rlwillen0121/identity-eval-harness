import { escapeHTML as esc, int, pct } from './format.js';
import { vendorLogo } from './logos.js';

const columns = [
  ['rank', '#', 'Published rank', 'asc'],
  ['name', 'Model', 'Model name', 'asc'],
  ['score', 'Score', 'Safety-adjusted score', 'desc'],
  ['resolved', 'Solves', 'Scenarios solved with zero safety errors', 'desc'],
  ['violations', 'Safety', 'Total overgrants and availability breaches', 'asc'],
];

const presets = [
  ['overall', 'Overall', 'rank', 'Ranked by safety-adjusted score. Exact scores share a rank.'],
  ['safety', 'Fewest safety errors', 'violations', 'Fewest safety errors first, then highest score.'],
];

function sortValue(entry, key) {
  switch (key) {
    case 'name': return entry.name;
    case 'score': return entry.safety_adjusted_score_tenths;
    case 'resolved': return entry.resolved_slots;
    case 'violations': return entry.og_total + entry.ab_total;
    default: return entry.rank;
  }
}

function compare(a, b, { key, dir }) {
  const av = sortValue(a, key);
  const bv = sortValue(b, key);
  const delta = key === 'name' ? av.localeCompare(bv, 'en') : av - bv;
  // Stable sort retains the publication's order within equal ranks.
  return (dir === 'desc' ? -delta : delta) || a.rank - b.rank;
}

function effortLabel(effort) {
  return effort === 'transport_default' ? 'Default' : effort;
}

function detailMarkup(entry) {
  const receipt = entry.probe_receipt_bound === undefined ? 'Status unavailable'
    : entry.probe_receipt_bound ? 'Bound probe receipt' : 'No bound probe receipt';
  const fields = (pairs) => pairs.map(([label, value]) =>
    `<dt>${esc(label)}</dt><dd>${esc(value)}</dd>`).join('');
  return `<tr class="detail-row"><td colspan="${columns.length}">
    <div class="model-detail" id="detail-${esc(entry.id)}" role="region" aria-label="${esc(entry.name)} run details">
      <div class="detail-columns">
        <div><h3>Run configuration</h3><dl class="detail-figures">${fields([
          ['Model ID', entry.model_id], ['Reasoning effort', effortLabel(entry.reasoning_effort)],
          ['Transport', entry.transport], ['Context probe', receipt],
        ])}</dl></div>
        <div><h3>Scored outcomes</h3><dl class="detail-figures">${fields([
          ['Overgrants', int(entry.og_total)], ['Availability breaches', int(entry.ab_total)],
          ['Turn limits', int(entry.step_limit_slots)], ['Parse failures', int(entry.parse_failure_slots)],
          ['Refusals', int(entry.refusal_slots)],
        ])}</dl></div>
      </div>
      ${entry.withheld_bundle_sha256 ? `<details class="tech-details"><summary>Withheld bundle SHA-256</summary><code>${esc(entry.withheld_bundle_sha256)}</code></details>` : ''}
    </div></td></tr>`;
}

function rowMarkup(entry, scenarios, rankLabel, expanded) {
  const errors = entry.og_total + entry.ab_total;
  const score = pct(entry.safety_adjusted_score_tenths / (10 * scenarios), 2);
  const safety = errors
    ? `<span class="safety-badge ${errors >= 15 ? 'is-high' : 'is-warn'}">${int(errors)} error${errors === 1 ? '' : 's'}</span><span class="safety-sub">${int(entry.og_total)} excess, ${int(entry.ab_total)} broken</span>`
    : '<span class="safety-badge is-clean"><span aria-hidden="true">✓</span> 0 errors</span>';
  return `<tr class="model-row${expanded ? ' is-selected' : ''}" id="model-${esc(entry.id)}" data-model-id="${esc(entry.id)}">
    <td class="rank-cell">${esc(rankLabel)}</td>
    <td class="identity-cell">${vendorLogo(entry.vendor, entry.vendor_label)}
      <button type="button" class="model-name" aria-label="${esc(entry.name)}, ${esc(effortLabel(entry.reasoning_effort))}, ${esc(entry.transport)}" aria-expanded="${expanded}"${expanded ? ` aria-controls="detail-${esc(entry.id)}"` : ''}>${esc(entry.name)}</button>
      <span class="model-subline">${esc(entry.vendor_label)} · ${esc(effortLabel(entry.reasoning_effort))}</span>
    </td>
    <td class="resolution-cell">${score}</td>
    <td class="tasks-cell"><span class="tasks-measure"><span>${int(entry.resolved_slots)}/${int(scenarios)}</span><span class="solve-meter" aria-hidden="true"><span style="width:${100 * entry.resolved_slots / scenarios}%"></span></span></span></td>
    <td class="unsafe-cell"><div class="safety-stack">${safety}</div></td>
  </tr>${expanded ? detailMarkup(entry) : ''}`;
}

export function mount(root, { entries, scenarios }, store) {
  const vendors = new Map();
  const ranks = new Map();
  for (const entry of entries) {
    const current = vendors.get(entry.vendor);
    vendors.set(entry.vendor, { label: entry.vendor_label, count: (current?.count ?? 0) + 1 });
    ranks.set(entry.rank, (ranks.get(entry.rank) ?? 0) + 1);
  }
  const providerButtons = [...vendors].sort((a, b) => a[1].label.localeCompare(b[1].label, 'en'))
    .map(([id, { label, count }]) => `<button class="vendor-button" type="button" data-vendor="${esc(id)}">
      ${vendorLogo(id, label)}${esc(label)} <span class="vendor-count">${int(count)}</span></button>`).join('');
  root.innerHTML = `
    <div class="leaderboard-controls">
      <label for="model-search" class="sr-only">Search model configurations</label>
      <input class="model-search" id="model-search" type="search" placeholder="Search models" autocomplete="off" spellcheck="false">
      <div class="vendor-controls" role="group" aria-label="Filter by provider">
        <button class="vendor-button" type="button" data-vendor="">All</button>${providerButtons}
      </div>
      <span class="model-count" role="status"></span>
    </div>
    <div class="table-subbar"><div class="view-controls" role="group" aria-label="Sort presets">
      ${presets.map(([id, label]) => `<button type="button" class="view-button" data-preset="${id}">${label}</button>`).join('')}
    </div><p class="table-key preset-note"></p></div>
    <table class="leaderboard-table explorer-table" aria-labelledby="leaderboard" aria-describedby="full-suite-summary full-suite-safety">
      <caption class="sr-only">Select a model to expand its run configuration and outcomes.</caption>
      <thead><tr>${columns.map(([key, label, title]) => `<th scope="col" data-key="${key}"><button class="sort-button" type="button" data-sort="${key}" aria-label="Sort by ${title.toLowerCase()}" title="${title}">${label}<span class="sort-indicator" aria-hidden="true"></span></button></th>`).join('')}</tr></thead>
      <tbody id="full-suite-rows"></tbody>
    </table>`;

  const body = root.querySelector('tbody');
  const search = root.querySelector('.model-search');

  function render({ query, vendors: activeVendors, sort, selected }) {
    const needle = query.trim().toLowerCase();
    const filtered = entries.filter((entry) =>
      (!activeVendors.size || activeVendors.has(entry.vendor)) &&
      [entry.name, entry.model_id, entry.vendor_label, entry.transport, entry.transport_id,
        entry.reasoning_effort, effortLabel(entry.reasoning_effort)].join(' ').toLowerCase().includes(needle));
    filtered.sort((a, b) => compare(a, b, sort));
    const active = presets.find(([, , key]) => sort.key === key && sort.dir === 'asc');
    root.querySelector('.preset-note').textContent = active?.[3] || 'Select a model for its run details.';
    root.querySelector('.model-count').textContent = `${int(filtered.length)} of ${int(entries.length)} configurations`;
    search.value = query;
    for (const button of root.querySelectorAll('[data-vendor]')) {
      const id = button.dataset.vendor;
      button.setAttribute('aria-pressed', String(id ? activeVendors.has(id) : !activeVendors.size));
    }
    for (const button of root.querySelectorAll('[data-preset]')) {
      button.setAttribute('aria-pressed', String(button.dataset.preset === active?.[0]));
    }
    for (const header of root.querySelectorAll('th[data-key]')) {
      const sorted = header.dataset.key === sort.key;
      if (sorted) header.setAttribute('aria-sort', sort.dir === 'asc' ? 'ascending' : 'descending');
      else header.removeAttribute('aria-sort');
      header.querySelector('.sort-indicator').textContent = sorted ? (sort.dir === 'asc' ? ' ↑' : ' ↓') : '';
    }
    body.innerHTML = filtered.map((entry) => rowMarkup(entry, scenarios,
      `${ranks.get(entry.rank) > 1 ? 'T' : ''}${int(entry.rank)}`, selected === entry.id)).join('')
      || `<tr class="empty-row"><td colspan="${columns.length}">No configurations match these filters.</td></tr>`;
  }

  search.addEventListener('input', () => store.set({ query: search.value, selected: null }));
  root.querySelector('.vendor-controls').addEventListener('click', (event) => {
    const button = event.target.closest('[data-vendor]');
    if (!button) return;
    const id = button.dataset.vendor;
    const next = new Set(store.get().vendors);
    if (!id) next.clear();
    else if (next.has(id)) next.delete(id);
    else next.add(id);
    store.set({ vendors: next, selected: null });
  });
  root.querySelector('.view-controls').addEventListener('click', (event) => {
    const preset = presets.find(([id]) => id === event.target.closest('[data-preset]')?.dataset.preset);
    if (preset) store.set({ sort: { key: preset[2], dir: 'asc' } });
  });
  root.querySelector('thead').addEventListener('click', (event) => {
    const key = event.target.closest('[data-sort]')?.dataset.sort;
    if (!key) return;
    const current = store.get().sort;
    const dir = current.key === key ? (current.dir === 'asc' ? 'desc' : 'asc')
      : columns.find(([field]) => field === key)[3];
    store.set({ sort: { key, dir } });
  });
  body.addEventListener('click', (event) => {
    const row = event.target.closest('.model-row');
    if (!row) return;
    const id = row.dataset.modelId;
    const keyboardFocus = document.activeElement === row.querySelector('.model-name');
    store.set({ selected: store.get().selected === id ? null : id });
    if (keyboardFocus) {
      [...body.querySelectorAll('.model-row')].find((r) => r.dataset.modelId === id)
        ?.querySelector('.model-name').focus({ preventScroll: true });
    }
  });
  render(store.get());
  return store.subscribe(render);
}
