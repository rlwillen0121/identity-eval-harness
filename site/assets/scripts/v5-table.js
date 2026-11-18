import { endToEndPct, pct, int, tokens, usd, escapeHTML as esc } from './format.js';
import { foldVendorChips, toggleVendorSelection, matchesModelQuery } from './table.js';
import { costBasis } from './detail.js';
import { mount as mountDetail } from './v5-detail.js';
import { vendorLogo } from './logos.js';

const arrow = (dir) => `<svg class="sort-arrow" viewBox="0 0 10 12" aria-hidden="true"><path d="${dir === 'asc' ? 'M5 10V2M2 5l3-3 3 3' : 'M5 2v8M2 7l3 3 3-3'}" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>`;
const OTHER_VENDOR = '__other__';
const clamp = (n) => Math.min(1, Math.max(0, Number(n) || 0));

function sortValue(model, key) {
  if (key === 'categories') {
    const rates = Object.values(model.categories || {}).map((c) => Number(c?.rate) || 0);
    return rates.length ? rates.reduce((a, b) => a + b, 0) / rates.length : 0;
  }
  if (key === 'safety') return (Number(model.overgrant_rate) || 0) + (Number(model.avail_breach_rate) || 0);
  if (key === 'failures') {
    const f = model.failures || {};
    return (Number(f.parse_failures) || 0) + (Number(f.turn_limits) || 0) + (Number(f.output_limits) || 0) + (Number(f.context_limits) || 0);
  }
  if (key === 'cost_usd') {
    if (model.cost_basis === 'local') return 0;
    return model.cost_usd == null ? Number.POSITIVE_INFINITY : Number(model.cost_usd);
  }
  return model[key];
}

export function compareV5Models(a, b, key, dir = 'asc') {
  const direction = dir === 'desc' ? -1 : 1;
  const av = sortValue(a, key);
  const bv = sortValue(b, key);
  const primary = typeof av === 'string' || typeof bv === 'string'
    ? String(av ?? '').localeCompare(String(bv ?? ''), 'en')
    : (Number(av) || 0) - (Number(bv) || 0);
  if (primary) return primary * direction;
  return (Number(a.rank) || 0) - (Number(b.rank) || 0);
}

export function v5SortComparator(key, dir = 'asc') {
  return (a, b) => compareV5Models(a, b, key, dir);
}

export const v5ColumnDefinitions = (data) => [
  ['rank', '#', 'Rank across the 18 protocol-complete runs by G-SRR descending, Sem-SRR@commit descending, safety breaches ascending, cost ascending, then model name.'],
  ['name', 'Model', 'Model display name, vendor, and reasoning effort.'],
  ['g_srr', 'G-SRR', `Grounded scenario resolution rate across all ${int(data.suite.slots_per_run)} slots (48 episodes × 3 repeats) with 95% 12-family cluster bootstrap CI.`],
  ['sem_srr_commit', 'Sem-SRR', 'Committed semantic resolution rate (Stages 1, 2, and 5), ignoring Stage 3 precondition and Stage 4 citation checks.'],
  ['categories', '7 Categories', 'Pass@1 across all 7 v5 categories including agent_governance.'],
  ['safety', 'Safety', 'Committed OVERGRANT (OG) and AVAIL_BREACH (AB) mean error rates across all 12 mutation variants.'],
  ['failures', 'Terminations', 'Distinct non-completed slot counts: parse failures, turn limits, output budget stops, and context limits (never lumped with provider errors).'],
  ['cost_usd', 'Cost', 'Billed provider cost when reported, otherwise retail token estimate. Partial accounting coverage is flagged.'],
];

function whisker(model) {
  const [lo, hi] = model.g_srr_ci.map((n) => clamp(n) * 120);
  const x = clamp(model.g_srr) * 120;
  const label = `95% cluster bootstrap interval: ${endToEndPct(model.g_srr_ci[0])} to ${endToEndPct(model.g_srr_ci[1])}`;
  return `<span class="resolution-measure"><span class="resolution-number${model.g_srr ? '' : ' muted'}">${endToEndPct(model.g_srr)}</span><span class="resolution-count muted" title="${int(model.g_srr_slots)} of ${int(model.planned_slots)} slots">${int(model.g_srr_slots)}</span><svg class="ci-whisker" viewBox="0 0 120 16" role="img" aria-label="${esc(label)}" title="${esc(label)}"><line class="ci-baseline" x1="0" x2="120" y1="8" y2="8"/><line x1="${lo}" x2="${hi}" y1="8" y2="8"/><line x1="${lo}" x2="${lo}" y1="5" y2="11"/><line x1="${hi}" x2="${hi}" y1="5" y2="11"/><circle cx="${x}" cy="8" r="2.5"/></svg></span>`;
}

export function v5FailureSummaryMarkup(failures) {
  const f = failures || {};
  const parts = [];
  if (f.provider_errors) parts.push(`<span class="failure-pill is-parse" title="Incomplete provider errors">${int(f.provider_errors)} prov</span>`);
  if (f.parse_failures) parts.push(`<span class="failure-pill is-parse" title="Within-protocol parse failures">${int(f.parse_failures)} parse</span>`);
  if (f.turn_limits) parts.push(`<span class="failure-pill" title="MAX_TURNS_EXCEEDED terminations">${int(f.turn_limits)} turn</span>`);
  if (f.output_limits) parts.push(`<span class="failure-pill" title="OUTPUT_BUDGET_STOP terminations">${int(f.output_limits)} out</span>`);
  if (f.context_limits) parts.push(`<span class="failure-pill" title="CONTEXT_LIMIT_EXCEEDED terminations">${int(f.context_limits)} ctx</span>`);
  return parts.length ? `<div class="failure-tags">${parts.join('')}</div>` : '<span class="failure-zero">0</span>';
}

export function v5ModelCellsMarkup(model, data, selected = false) {
  const og = Number(model.overgrant_rate) || 0;
  const ab = Number(model.avail_breach_rate) || 0;
  const costCell = model.cost_basis === 'local'
    ? '<span class="muted">local</span>'
    : `${usd(model.cost_usd)}${model.accounting_coverage !== 'COMPLETE' ? '<span class="cost-partial">partial</span>' : ''}`;
  const costTitle = model.cost_basis === 'local'
    ? (model.recorded_estimated_cost_usd ? `Local hardware (audit token estimate ${usd(model.recorded_estimated_cost_usd)})` : costBasis['local'])
    : `${costBasis[model.cost_basis] || 'Cost'} (${model.accounting_coverage || 'COMPLETE'} accounting)`;

  return `
    <td class="rank-cell">${int(model.rank)}</td>
    <td class="identity-cell"><button type="button" class="model-name" aria-expanded="${selected}"${selected ? ` aria-controls="detail-${esc(model.id)}"` : ''}>${vendorLogo(model.vendor, model.vendor_label)}${esc(model.name)}</button><span class="model-attribution"><span class="vendor-label">${esc(model.vendor_label)}<span class="effort-tag">${esc(model.effort)}</span></span></span></td>
    <td class="resolution-cell">${whisker(model)}</td>
    <td class="desktop-col"><span class="sem-measure"><span class="sem-number">${pct(model.sem_srr_commit)}</span><span class="sem-track" aria-hidden="true"><span class="sem-fill" style="width:${clamp(model.sem_srr_commit) * 100}%"></span></span></span></td>
    <td class="desktop-col"><div class="category-bars v5-category-bars">${data.categories.map((cat) => {
      const c = model.categories?.[cat.id] || { pass_count: 0, planned_slots: model.planned_slots, rate: 0 };
      const label = `${cat.label} ${int(c.pass_count)}/${int(c.planned_slots)} (${pct(c.rate)})`;
      return `<span class="category-track" role="img" aria-label="${esc(label)}" title="${esc(label)}"><span class="category-fill" style="height:${clamp(c.rate) * 100}%"></span></span>`;
    }).join('')}</div></td>
    <td class="desktop-col"><div class="safety-pair"><span title="OVERGRANT@commit mean (${int(model.overgrant_count)} slots)"><span class="safety-label">OG</span><b class="${og > 0.05 ? 'is-breach' : ''}">${pct(og)}</b></span><span title="AVAIL_BREACH@commit mean (${int(model.avail_breach_count)} slots)"><span class="safety-label">AB</span><b class="${ab > 0.25 ? 'is-breach' : ''}">${pct(ab)}</b></span></div></td>
    <td class="desktop-col">${v5FailureSummaryMarkup(model.failures)}</td>
    <td class="desktop-col cost-cell" title="${esc(costTitle)}">${costCell}</td>`;
}

export function mount(rootEl, data, store) {
  const vendorChips = foldVendorChips(data.vendors);
  const columns = v5ColumnDefinitions(data);
  rootEl.innerHTML = `
    <div class="leaderboard-controls">
      <label class="sr-only" for="v5-model-search">Filter complete v5 models</label>
      <input class="model-search" id="v5-model-search" type="search" placeholder="Filter 18 complete configurations" autocomplete="off" spellcheck="false">
      <div class="vendor-controls" role="group" aria-label="Filter by vendor">
        <button class="vendor-button" type="button" data-vendor="" aria-pressed="true">All</button>
        ${vendorChips.map((chip) => `<button class="vendor-button" type="button"${chip.id === OTHER_VENDOR ? ' data-vendor-other' : ` data-vendor="${esc(chip.id)}"`} aria-pressed="false">${chip.kind === 'vendor' ? vendorLogo(chip.id, chip.label) : ''}${esc(chip.label)} <span class="vendor-count muted">${int(chip.count)}</span></button>`).join('')}
      </div>
    </div>
    <p class="table-key"><span class="category-key">7 Categories: ${data.categories.map((c) => esc(c.label)).join(' · ')}</span><span class="model-count" role="status" aria-live="polite"></span></p>
    <table class="leaderboard-table v5-table">
      <caption class="sr-only">Preliminary v5_core protocol-complete model configurations. Activate a model row to inspect full 7-category, family, safety, and failure metrics.</caption>
      <thead><tr>${columns.map(([key, label, title]) => `<th scope="col" data-key="${key}"${['sem_srr_commit', 'categories', 'safety', 'failures', 'cost_usd'].includes(key) ? ' class="desktop-col"' : ''}><button type="button" class="sort-button" data-sort="${key}" title="${esc(title)}">${label}<span class="sort-indicator"></span></button></th>`).join('')}</tr></thead>
      <tbody></tbody>
    </table>`;

  const body = rootEl.querySelector('.leaderboard-table > tbody');
  const search = rootEl.querySelector('.model-search');
  const count = rootEl.querySelector('.model-count');
  let previous;
  let cleanupDetail;

  function highlight() {
    const { hover, selected } = store.get();
    body.querySelectorAll('.model-row').forEach((row) => {
      row.classList.toggle('is-highlighted', row.dataset.modelId === hover);
      row.classList.toggle('is-selected', row.dataset.modelId === selected);
    });
  }

  function render() {
    const state = store.get();
    const signature = JSON.stringify([state.sort, [...state.vendors].sort(), state.query, state.selected]);
    if (signature === previous) { highlight(); return; }
    previous = signature;
    const focusedId = document.activeElement?.closest?.('.model-row')?.dataset?.modelId;
    cleanupDetail?.();
    const models = data.models.filter((m) => (!state.vendors.size || state.vendors.has(m.vendor)) && matchesModelQuery(m, state.query));
    models.sort(v5SortComparator(state.sort.key, state.sort.dir));
    search.value = state.query;
    count.textContent = `${int(models.length)}${models.length === data.models.length ? '' : ` of ${int(data.models.length)}`} complete configurations`;
    rootEl.querySelectorAll('[data-vendor], [data-vendor-other]').forEach((btn) => {
      let pressed;
      if (btn.hasAttribute('data-vendor-other')) {
        const foldedIds = vendorChips.find((chip) => chip.id === OTHER_VENDOR)?.vendorIds || [];
        pressed = foldedIds.length > 0 && foldedIds.every((id) => state.vendors.has(id));
      } else {
        const id = btn.dataset.vendor;
        pressed = id ? state.vendors.has(id) : !state.vendors.size;
      }
      btn.setAttribute('aria-pressed', String(pressed));
    });
    rootEl.querySelectorAll('th[data-key]').forEach((th) => {
      const active = th.dataset.key === state.sort.key;
      th.setAttribute('aria-sort', active ? (state.sort.dir === 'asc' ? 'ascending' : 'descending') : 'none');
      th.querySelector('.sort-indicator').innerHTML = active ? arrow(state.sort.dir) : '';
    });
    body.innerHTML = models.map((model) => {
      const selected = model.id === state.selected;
      return `<tr class="model-row" id="model-${esc(model.id)}" data-model-id="${esc(model.id)}">${v5ModelCellsMarkup(model, data, selected)}</tr>${selected ? `<tr class="detail-row"><td colspan="${columns.length}"><div class="model-detail" id="detail-${esc(model.id)}" role="region" aria-label="${esc(model.name)} detail"></div></td></tr>` : ''}`;
    }).join('') || `<tr class="empty-row"><td colspan="${columns.length}">No complete configurations match these filters. Clear the search or choose All vendors.</td></tr>`;
    const detail = body.querySelector('.model-detail');
    if (detail) cleanupDetail = mountDetail(detail, data, store);
    highlight();
    if (focusedId) [...body.querySelectorAll('.model-row')].find((row) => row.dataset.modelId === focusedId)?.querySelector('.model-name')?.focus({ preventScroll: true });
  }

  search.addEventListener('input', () => store.set({ query: search.value }));
  search.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && search.value) { e.preventDefault(); search.value = ''; store.set({ query: '' }); }
  });
  rootEl.querySelector('.vendor-controls').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-vendor], [data-vendor-other]');
    if (!btn) return;
    const chip = btn.hasAttribute('data-vendor-other')
      ? vendorChips.find((item) => item.kind === 'other')
      : { id: btn.dataset.vendor, kind: btn.dataset.vendor ? 'vendor' : 'all' };
    if (chip) store.set({ vendors: toggleVendorSelection(store.get().vendors, chip) });
  });
  rootEl.querySelector('thead').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-sort]');
    if (!btn) return;
    const key = btn.dataset.sort;
    const current = store.get().sort;
    const dir = current.key === key ? (current.dir === 'asc' ? 'desc' : 'asc')
      : (['g_srr', 'sem_srr_commit', 'categories'].includes(key) ? 'desc' : 'asc');
    store.set({ sort: { key, dir } });
  });
  const modelRow = (target) => (typeof target?.closest === 'function' ? target.closest('.model-row') : null);
  body.addEventListener('click', (e) => {
    const row = modelRow(e.target);
    if (row) store.set({ selected: store.get().selected === row.dataset.modelId ? null : row.dataset.modelId });
  });
  for (const eventName of ['mouseover', 'focusin']) {
    body.addEventListener(eventName, (e) => {
      const row = modelRow(e.target);
      if (row && row !== modelRow(e.relatedTarget)) store.set({ hover: row.dataset.modelId });
    });
  }
  for (const eventName of ['mouseout', 'focusout']) {
    body.addEventListener(eventName, (e) => {
      const row = modelRow(e.target);
      if (row && row !== modelRow(e.relatedTarget) && store.get().hover === row.dataset.modelId) store.set({ hover: null });
    });
  }
  render();
  const unsubscribe = store.subscribe(render);
  return () => { unsubscribe(); cleanupDetail?.(); };
}

export function mountIncomplete(rootEl, data) {
  const runs = Array.isArray(data.incomplete_runs) ? data.incomplete_runs : [];
  rootEl.innerHTML = `
    <div class="incomplete-callout">
      <strong>Not comparable / excluded from ranking (${int(runs.length)} runs):</strong> Each run below terminated early on infrastructure provider errors (<code>INCOMPLETE_PROVIDER_FAILURE</code>). Headline scores remain <code>UNMEASURED</code> and are never zero-imputed.
    </div>
    <div class="incomplete-wrap" tabindex="0" role="region" aria-label="Provider-incomplete v5 configurations">
      <table class="incomplete-table">
        <caption class="sr-only">Provider-incomplete v5_core runs excluded from ranking</caption>
        <thead>
          <tr>
            <th scope="col">Configuration</th>
            <th scope="col">Observed / Planned</th>
            <th scope="col">Exec-valid rate</th>
            <th scope="col">Provider errors</th>
            <th scope="col">Parse failures</th>
            <th scope="col">Turn limits</th>
            <th scope="col">Other stops</th>
            <th scope="col">Provisional G-SRR [L, U]</th>
          </tr>
        </thead>
        <tbody>
          ${runs.map((run) => {
            const f = run.failures || {};
            const otherStops = [];
            if (f.output_limits) otherStops.push(`${int(f.output_limits)} out`);
            if (f.context_limits) otherStops.push(`${int(f.context_limits)} ctx`);
            const bounds = Array.isArray(run.provisional_bounds)
              ? `[${pct(run.provisional_bounds[0])}, ${pct(run.provisional_bounds[1])}]`
              : 'UNMEASURED';
            return `<tr>
              <td><span class="incomplete-name">${vendorLogo(run.vendor, run.vendor_label)}${esc(run.name)} <span class="effort-tag">${esc(run.effort)}</span></span><span class="incomplete-sub">${esc(run.run_id)} · ${esc(run.vendor_label)} · ${tokens(run.output_tokens)} out tok</span></td>
              <td>${int(run.observed_slots)}/${int(run.planned_slots)} <span class="muted">(${pct(run.completion_coverage)})</span></td>
              <td>${pct(run.execution_valid_rate)}</td>
              <td class="provider-err-count">${int(f.provider_errors)}</td>
              <td>${int(f.parse_failures)}</td>
              <td>${int(f.turn_limits)}</td>
              <td>${otherStops.length ? esc(otherStops.join(', ')) : '<span class="muted">0</span>'}</td>
              <td><span class="unmeasured-badge">UNMEASURED</span> <span class="muted">${esc(bounds)}</span></td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    </div>`;
}
