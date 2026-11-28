import { compact, duration, escapeHTML as esc, int, pct, usd } from './format.js';
import { effortLabel, matchesConfiguration } from './filter.js';
import { vendorLogo } from './logos.js';

const columns = [
  ['rank', '#', 'Published rank', 'asc'],
  ['name', 'Model', 'Model name', 'asc'],
  ['score', 'Score', 'Safety-adjusted score', 'desc'],
  ['violations', 'Safety errors', 'Total overgrants and availability breaches', 'asc'],
  ['tokens', 'Tokens', 'Total recorded tokens', 'asc'],
  ['cost', 'Est. cost', 'Estimated retail cost of the run', 'asc'],
  ['time', 'Time / task', 'Mean active time per scenario', 'asc'],
];

function sortValue(entry, key) {
  switch (key) {
    case 'name': return entry.name;
    case 'score': return entry.safety_adjusted_score_tenths;
    case 'tokens': return entry.analysis?.total_tokens;
    case 'cost': return entry.analysis?.estimated_cost_pricing_sha256 ? entry.analysis.estimated_cost_usd : null;
    case 'time': return entry.analysis?.mean_task_time_s;
    case 'violations': return entry.og_total + entry.ab_total;
    default: return entry.rank;
  }
}

function compare(a, b, { key, dir }) {
  const av = sortValue(a, key);
  const bv = sortValue(b, key);
  if (av == null || bv == null) return Number(av == null) - Number(bv == null) || a.rank - b.rank;
  const delta = key === 'name' ? av.localeCompare(bv, 'en') : av - bv;
  // Stable sort retains the publication's order within equal ranks.
  return (dir === 'desc' ? -delta : delta) || a.rank - b.rank;
}

function detailMarkup(entry, scenarios) {
  const receipt = entry.probe_receipt_bound === undefined ? 'Status unavailable'
    : entry.probe_receipt_bound ? 'Verified for this run' : 'No check linked to this run';
  const fields = (pairs) => pairs.map(([label, value]) =>
    `<dt>${esc(label)}</dt><dd>${esc(value)}</dd>`).join('');
  return `<tr role="row" class="detail-row"><td colspan="${columns.length}">
    <div class="model-detail" id="detail-${esc(entry.id)}" role="region" aria-label="${esc(entry.name)} run details">
      <dl class="expanded-metrics">${fields([
        ['Safety errors', int(sortValue(entry, 'violations'))], ['Total tokens', int(sortValue(entry, 'tokens'))],
        ['Est. run cost', usd(sortValue(entry, 'cost'))], ['Time / task', duration(sortValue(entry, 'time'))],
      ])}</dl>
      <div class="detail-columns">
        <div><h3>How the model ran</h3><dl class="detail-figures">${fields([
          ['Model ID', entry.model_id], ['Requested reasoning effort', effortLabel(entry.reasoning_effort)],
          ['Connection', entry.transport], ['Model connection check', receipt],
        ])}</dl></div>
        <div><h3>What happened</h3><dl class="detail-figures">${fields([
          ['Clean solves', `${int(entry.resolved_slots)}/${int(scenarios)}`],
          ['Excess access', int(entry.og_total)], ['Availability breaches', int(entry.ab_total)],
          ['Turn limits', int(entry.step_limit_slots)], ['Parse failures', int(entry.parse_failure_slots)],
          ['Refusals', int(entry.refusal_slots)],
        ])}</dl></div>
      </div>
      ${entry.withheld_bundle_sha256 ? `<details class="tech-details"><summary>Private run record (SHA-256)</summary><code>${esc(entry.withheld_bundle_sha256)}</code></details>` : ''}
    </div></td></tr>`;
}

function rowMarkup(entry, scenarios, rankLabel, expanded, mobileMetric) {
  const visibleMetric = (key) => key === mobileMetric ? ' mobile-result' : '';
  const tokens = sortValue(entry, 'tokens');
  const cost = sortValue(entry, 'cost');
  const time = sortValue(entry, 'time');
  const errors = entry.og_total + entry.ab_total;
  const score = entry.safety_adjusted_score_tenths / (10 * scenarios);
  return `<tr role="row" class="model-row${expanded ? ' is-selected' : ''}" id="model-${esc(entry.id)}" data-model-id="${esc(entry.id)}">
    <td class="rank-cell">${esc(rankLabel)}</td>
    <td class="identity-cell">${vendorLogo(entry.vendor, entry.vendor_label)}
      <button type="button" class="model-name" aria-label="${esc(entry.name)}, ${esc(effortLabel(entry.reasoning_effort))} effort requested, ${esc(entry.transport)}" aria-expanded="${expanded}"${expanded ? ` aria-controls="detail-${esc(entry.id)}"` : ''}>${esc(entry.name)}</button>
      <span class="model-subline">${esc(entry.vendor_label)} · ${esc(effortLabel(entry.reasoning_effort))} effort requested</span>
    </td>
    <td class="resolution-cell${visibleMetric('score')}"><span class="score-value${score < 0 ? ' negative' : ''}">${pct(score, 2).replace('-', '−')}</span></td>
    <td class="error-count${visibleMetric('violations')}">${int(errors)}</td>
    <td class="usage-cell${visibleMetric('tokens')}" title="${int(tokens)} total tokens">${compact(tokens)}</td>
    <td class="usage-cell${visibleMetric('cost')}">${usd(cost)}</td>
    <td class="usage-cell${visibleMetric('time')}">${duration(time)}</td>
  </tr>${expanded ? detailMarkup(entry, scenarios) : ''}`;
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
      <label class="mobile-sort">Sort by
        <select class="sort-select" aria-label="Sort leaderboard">${columns.map(([key, label]) => `<option value="${key}">${({ rank: 'Rank', score: 'Score' })[key] || label}</option>`).join('')}</select>
      </label>
    </div>
    <table role="table" class="leaderboard-table explorer-table" aria-label="Full-suite leaderboard" aria-describedby="full-suite-summary">
      <caption class="sr-only">Select a model to expand its run configuration and outcomes.</caption>
      <thead><tr>${columns.map(([key, label, title]) => `<th scope="col" data-key="${key}"><button class="sort-button" type="button" data-sort="${key}" aria-label="Sort by ${title.toLowerCase()}" title="${title}">${label}<span class="sort-indicator" aria-hidden="true"></span></button></th>`).join('')}</tr></thead>
      <tbody role="rowgroup" id="full-suite-rows"></tbody>
    </table>`;

  const body = root.querySelector('tbody');
  const search = root.querySelector('.model-search');

  function render({ query, vendors: activeVendors, sort, selected }) {
    const filtered = entries.filter((entry) => matchesConfiguration(entry, { query, vendors: activeVendors }));
    filtered.sort((a, b) => compare(a, b, sort));
    root.querySelector('.model-count').textContent = filtered.length === entries.length
      ? `${int(entries.length)} runs` : `${int(filtered.length)} of ${int(entries.length)} runs`;
    root.querySelector('.sort-select').value = sort.key;
    search.value = query;
    for (const button of root.querySelectorAll('[data-vendor]')) {
      const id = button.dataset.vendor;
      button.setAttribute('aria-pressed', String(id ? activeVendors.has(id) : !activeVendors.size));
    }
    const mobileMetric = ['rank', 'name'].includes(sort.key) ? 'score' : sort.key;
    for (const header of root.querySelectorAll('th[data-key]')) {
      header.classList.toggle('mobile-result', header.dataset.key === mobileMetric);
      const sorted = header.dataset.key === sort.key;
      if (sorted) header.setAttribute('aria-sort', sort.dir === 'asc' ? 'ascending' : 'descending');
      else header.removeAttribute('aria-sort');
      header.querySelector('.sort-indicator').textContent = sorted ? (sort.dir === 'asc' ? ' ↑' : ' ↓') : '';
    }
    body.innerHTML = filtered.map((entry) => rowMarkup(entry, scenarios,
      `${ranks.get(entry.rank) > 1 ? 'T' : ''}${int(entry.rank)}`, selected === entry.id, mobileMetric)).join('')
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
  root.querySelector('.sort-select').addEventListener('change', (event) => {
    const column = columns.find(([key]) => key === event.target.value);
    if (column) store.set({ sort: { key: column[0], dir: column[3] } });
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
  return store.subscribe((state, patch) => {
    if (Object.keys(patch).some((key) => key !== 'hover')) render(state);
  });
}
