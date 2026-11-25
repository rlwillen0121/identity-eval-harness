import { gSrrPct, pct, int, num, usd, compact, duration, formatInterval, tokPerSec, escapeHTML as esc } from './format.js';
import { mount as mountDetail } from './detail.js';
import { vendorLogo } from './logos.js';

const arrow = (dir) => `<svg class="sort-arrow" viewBox="0 0 10 12" aria-hidden="true"><path d="${dir === 'asc' ? 'M5 10V2M2 5l3-3 3 3' : 'M5 2v8M2 7l3 3 3-3'}" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>`;
const checkGlyph = () => '<svg class="clean-check" viewBox="0 0 12 12" aria-hidden="true"><path d="M2.2 6.3 4.8 8.9 9.8 3.3" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const clamp = (n) => Math.min(1, Math.max(0, Number(n) || 0));

const QUALITY_FLOOR = 0.8;

export function matchesModelQuery(model, query) {
  const needle = String(query ?? '').trim().toLocaleLowerCase('en-US');
  if (!needle) return true;
  return `${model.name ?? ''} ${model.vendor_label ?? ''} ${model.model_id ?? ''} ${model.transport_label ?? ''}`
    .toLocaleLowerCase('en-US')
    .includes(needle);
}

function categoryMean(model) {
  const rates = Object.values(model.scores?.category_pass_rates || {}).filter((v) => typeof v === 'number' && Number.isFinite(v));
  if (!rates.length) return 0;
  return rates.reduce((sum, v) => sum + v, 0) / rates.length;
}

function totalViolations(model) {
  return (Number(model.scores?.og_total) || 0) + (Number(model.scores?.ab_total) || 0);
}

function positiveOrInf(value) {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n : Number.POSITIVE_INFINITY;
}

function meetsQualityFloor(model) {
  return (Number(model.scores?.g_srr) || 0) >= QUALITY_FLOOR;
}

function tieSignature(model) {
  const s = model.scores || {};
  return [
    Number(s.g_srr ?? -1),
    Number(s.macro_cat_acc ?? -1),
    Number(s.commit_rate ?? -1),
    Number(s.violations_per_committed_slot ?? -1),
  ].join('|');
}

function computeTieMeta(models) {
  const counts = new Map();
  const minRank = new Map();
  for (const m of models) {
    const sig = tieSignature(m);
    counts.set(sig, (counts.get(sig) || 0) + 1);
    const r = Number(m.rank) || 0;
    if (!minRank.has(sig) || r < minRank.get(sig)) minRank.set(sig, r);
  }
  const labels = new Map();
  const groupRank = new Map();
  for (const m of models) {
    const sig = tieSignature(m);
    const r = minRank.get(sig) || Number(m.rank) || 0;
    groupRank.set(m.id, r);
    labels.set(m.id, counts.get(sig) > 1 ? `T${r}` : int(m.rank));
  }
  return { labels, groupRank };
}

function sortValue(model, key) {
  switch (key) {
    case 'rank': return Number(model.rank) || 0;
    case 'name': return model.name || '';
    case 'g_srr': return Number(model.scores?.g_srr) || 0;
    case 'macro_cat_acc': return Number(model.scores?.macro_cat_acc) || 0;
    case 'categories': return categoryMean(model);
    case 'violations': return totalViolations(model);
    case 'speed': return positiveOrInf(model.diagnostics?.median_slot_wall_time_s);
    case 'cost_usd': return positiveOrInf(model.usage?.estimated_cost_usd);
    case 'cost_per_resolved': return positiveOrInf(model.usage?.cost_per_resolved_slot_usd);
    default: return Number(model.rank) || 0;
  }
}

export function compareModels(a, b, key, dir = 'asc', tieMeta = null) {
  const direction = dir === 'desc' ? -1 : 1;

  if (key === 'rank' && tieMeta) {
    const grDiff = (tieMeta.groupRank.get(a.id) ?? a.rank) - (tieMeta.groupRank.get(b.id) ?? b.rank);
    if (grDiff) return grDiff * direction;
    const costDiff = positiveOrInf(a.usage?.estimated_cost_usd) - positiveOrInf(b.usage?.estimated_cost_usd);
    if (costDiff) return costDiff * direction;
    const speedDiff = positiveOrInf(a.diagnostics?.median_slot_wall_time_s) - positiveOrInf(b.diagnostics?.median_slot_wall_time_s);
    if (speedDiff) return speedDiff * direction;
    return (Number(a.rank) || 0) - (Number(b.rank) || 0);
  }

  if (key === 'cost_per_resolved' || key === 'fastest_tier') {
    const aQual = meetsQualityFloor(a) ? 0 : 1;
    const bQual = meetsQualityFloor(b) ? 0 : 1;
    if (aQual !== bQual) return aQual - bQual;
    const metricKey = key === 'fastest_tier' ? 'speed' : 'cost_per_resolved';
    const diff = sortValue(a, metricKey) - sortValue(b, metricKey);
    if (diff) return diff * direction;
    const gDiff = (Number(b.scores?.g_srr) || 0) - (Number(a.scores?.g_srr) || 0);
    if (gDiff) return gDiff;
    return (Number(a.rank) || 0) - (Number(b.rank) || 0);
  }

  const av = sortValue(a, key);
  const bv = sortValue(b, key);
  const primary = typeof av === 'string' || typeof bv === 'string'
    ? String(av).localeCompare(String(bv), 'en')
    : av - bv;
  if (primary) return primary * direction;
  if (key === 'violations' || key === 'speed' || key === 'cost_usd') {
    const gDiff = (Number(b.scores?.g_srr) || 0) - (Number(a.scores?.g_srr) || 0);
    if (gDiff) return gDiff;
  }
  return (Number(a.rank) || 0) - (Number(b.rank) || 0);
}

export const columnDefinitions = () => [
  ['rank', '#', 'Overall rank by end-to-end score, domain average, completion rate, and lowest safety errors. Exact ties share a rank and are listed lower cost first.'],
  ['name', 'Model', 'Model name, provider, and reasoning effort. Click any row for full domain and scenario details.'],
  ['g_srr', 'End to end', 'Share of 48 scenarios solved with every required domain check passed and zero safety errors, with a 95% confidence range.'],
  ['macro_cat_acc', 'Domain avg', 'Average accuracy across the IAM domains tested in each scenario.'],
  ['categories', 'By domain', 'Exact pass rate across all eight IAM domains. Hover any bar to read that domain score.'],
  ['violations', 'Safety', 'Total safety errors across all 48 scenarios: excess (unauthorized access left behind) and broken (legitimate access disrupted).'],
  ['speed', 'Time / task', 'Median wall-clock time to complete one scenario (per task, not total run time). Lower is faster.'],
  ['cost_usd', 'Est. cost', 'Estimated USD for a full 48-scenario run under one shared retail pricing table. Only orders exact ties.'],
];

const VIEW_PRESETS = [
  { id: 'overall', label: 'Overall', sort: { key: 'rank', dir: 'asc' }, note: 'Exact ties share a rank (T1), listed lower cost first.' },
  { id: 'safety', label: 'Safest first', sort: { key: 'violations', dir: 'asc' }, note: 'Sorted by fewest safety errors, then highest end-to-end score.' },
  { id: 'value', label: 'Best value', sort: { key: 'cost_per_resolved', dir: 'asc' }, note: 'Models solving at least 80% first, sorted by estimated $/solved.' },
  { id: 'fastest', label: 'Fastest', sort: { key: 'fastest_tier', dir: 'asc' }, note: 'Models solving at least 80% first, sorted by median time per scenario.' },
];

function intervalGraphic(model) {
  const iv = model.scores?.g_srr_interval;
  if (!iv) return '';
  const lo = clamp(iv.lo) * 96;
  const hi = clamp(iv.hi) * 96;
  const x = clamp(model.scores?.g_srr) * 96;
  const label = `95% confidence range: ${pct(iv.lo, 1)} to ${pct(iv.hi, 1)}`;
  return `<svg class="ci-whisker" viewBox="0 0 96 16" role="img" aria-label="${esc(label)}"><title>${esc(label)}</title><line class="ci-baseline" x1="0" x2="96" y1="8" y2="8"/><line x1="${lo}" x2="${hi}" y1="8" y2="8"/><line x1="${lo}" x2="${lo}" y1="5" y2="11"/><line x1="${hi}" x2="${hi}" y1="5" y2="11"/><circle cx="${x}" cy="8" r="2.5"/></svg>`;
}

function categoryBarsMarkup(model, categories) {
  const rates = model.scores?.category_pass_rates || {};
  return `<div class="category-bars">${categories.map((cat) => {
    const rate = rates[cat.id];
    const valid = typeof rate === 'number' && Number.isFinite(rate);
    const fillPct = valid ? clamp(rate) * 100 : 0;
    const isFull = valid && rate >= 0.9999;
    const label = `${cat.title}: ${valid ? pct(rate, 0) : 'n/a'}`;
    return `<span class="category-track" role="img" aria-label="${esc(label)}" title="${esc(label)}"><span class="category-fill${isFull ? ' is-full' : ''}" style="height:${fillPct}%"></span></span>`;
  }).join('')}</div>`;
}

function safetyDetail(og, ab) {
  if (og > 0 && ab > 0) return `${int(og)} excess, ${int(ab)} broken`;
  if (og > 0) return `${int(og)} excess`;
  return `${int(ab)} broken`;
}

function safetyCellMarkup(model) {
  const s = model.scores || {};
  const og = Number(s.og_total) || 0;
  const ab = Number(s.ab_total) || 0;
  const total = og + ab;
  if (total === 0) {
    return `<span class="safety-badge is-clean" title="Zero excess access and zero broken access across all 48 scenarios">${checkGlyph()}Clean</span>`;
  }
  const severity = total >= 15 ? 'is-high' : 'is-warn';
  const countLabel = `${int(total)} error${total === 1 ? '' : 's'}`;
  const detail = safetyDetail(og, ab);
  const title = `${int(og)} overgrants (unauthorized access) and ${int(ab)} availability breaches (broken access)`;
  return `<div class="safety-stack" title="${esc(title)}"><span class="safety-badge ${severity}">${esc(countLabel)}</span><span class="safety-sub">${esc(detail)}</span></div>`;
}

function mobileHighlightMarkup(model, sortKey) {
  const u = model.usage || {};
  const d = model.diagnostics || {};
  if (sortKey === 'cost_per_resolved') {
    return `<span class="mobile-metric">${esc(usd(u.cost_per_resolved_slot_usd))}/solved</span>`;
  }
  if (sortKey === 'fastest_tier' || sortKey === 'speed') {
    return `<span class="mobile-metric">${esc(duration(d.median_slot_wall_time_s))}</span>`;
  }
  if (sortKey === 'cost_usd') {
    return `<span class="mobile-metric">${esc(usd(u.estimated_cost_usd))}</span>`;
  }
  return '';
}

export function modelCellsMarkup(model, selected = false, options = {}) {
  const { categories = [], maxViolations = 1, rankLabel = int(model.rank), sortKey = 'rank' } = options;
  const s = model.scores || {};
  const d = model.diagnostics || {};
  const u = model.usage || {};
  const rankTitle = s.rank_interval
    ? `Rank #${int(model.rank)} (95% rank range: ${formatInterval(s.rank_interval, false)})`
    : `Rank #${int(model.rank)}`;
  const speedText = duration(d.median_slot_wall_time_s);
  const speedTitle = `Median time per scenario: ${speedText} (${num(d.mean_turns, 1)} avg turns, ${tokPerSec(d.generation_tokens_per_second)})`;
  const showUnitCost = sortKey === 'cost_per_resolved';
  const costPrimary = showUnitCost ? `${usd(u.cost_per_resolved_slot_usd)}/solved` : usd(u.estimated_cost_usd);
  const totalTok = (Number(u.uncached_input_tokens) || 0) + (Number(u.cache_read_tokens) || 0) + (Number(u.cache_write_tokens) || 0) + (Number(u.output_tokens) || 0);
  const tokSub = totalTok > 0 ? `${compact(totalTok)} tok` : '';
  const costTitle = `Estimated total: ${usd(u.estimated_cost_usd)} (${usd(u.cost_per_resolved_slot_usd)} per solved) · ${compact(totalTok)} total tokens (${compact(u.output_tokens)} output/thinking)`;
  const effortText = model.reasoning_effort && model.reasoning_effort !== 'transport_default'
    ? ` · ${model.reasoning_effort}`
    : '';
  const mobileExtra = mobileHighlightMarkup(model, sortKey);
  return `
    <td class="rank-cell" title="${esc(rankTitle)}">${esc(rankLabel)}</td>
    <td class="identity-cell">
      <button type="button" class="model-name" aria-expanded="${selected}"${selected ? ` aria-controls="detail-${esc(model.id)}"` : ''}>
        ${vendorLogo(model.vendor, model.vendor_label)}${esc(model.name)}
      </button>
      <div class="model-subline">
        <span>${esc(model.vendor_label)}${esc(effortText)}</span>${mobileExtra}
      </div>
    </td>
    <td class="resolution-cell"><span class="resolution-measure"><span class="resolution-number">${gSrrPct(s.g_srr)}</span><span class="resolution-count">${int(s.resolved_slots)}/${int(s.planned_slots)}</span>${intervalGraphic(model)}</span></td>
    <td class="desktop-col tasks-cell"><span class="tasks-measure"><span class="tasks-number">${pct(s.macro_cat_acc, 1)}</span><span class="tasks-track" aria-hidden="true"><span class="tasks-fill" style="width:${clamp(s.macro_cat_acc) * 100}%"></span></span></span></td>
    <td class="desktop-col">${categoryBarsMarkup(model, categories)}</td>
    <td class="unsafe-cell">${safetyCellMarkup(model)}</td>
    <td class="desktop-col speed-cell" title="${esc(speedTitle)}">${esc(speedText)}</td>
    <td class="desktop-col cost-cell" title="${esc(costTitle)}"><div class="cost-stack"><span>${esc(costPrimary)}</span>${tokSub ? `<span class="cost-sub">${esc(tokSub)}</span>` : ''}</div></td>
  `;
}

function renderNonRankedSections(data) {
  const parts = [];
  const pending = data.pending || [];
  const unranked = data.unranked || [];
  const panel = data.reliability_panel;

  if (pending.length) {
    parts.push(`
      <div class="subboard">
        <h3>Runs in progress</h3>
        <table class="subboard-table">
          <thead><tr><th>Model</th><th>Completed</th><th>Current score</th><th>Possible range</th><th>Avg turns</th></tr></thead>
          <tbody>${pending.map((m) => {
            const b = m.scores?.provisional_g_srr_bounds;
            const boundsText = Array.isArray(b) ? `${pct(b[0], 1)} to ${pct(b[1], 1)}` : 'n/a';
            return `<tr><td>${esc(m.name)} <span class="muted">(${esc(m.vendor_label)})</span></td><td>${int(m.scores?.observed_slots)}/${int(m.scores?.planned_slots)}</td><td>${pct(m.scores?.g_srr ?? null, 1)}</td><td>${boundsText}</td><td>${num(m.diagnostics?.mean_turns, 1)}</td></tr>`;
          }).join('')}</tbody>
        </table>
      </div>
    `);
  }

  if (unranked.length) {
    parts.push(`
      <div class="subboard">
        <h3>Completed runs not ranked</h3>
        <table class="subboard-table">
          <thead><tr><th>Model</th><th>End to end</th><th>Domain avg</th><th>Notes</th></tr></thead>
          <tbody>${unranked.map((m) => {
            const reasons = (m.eligibility?.reasons || []).map((r) => r.detail || r.code).join(', ') || 'Did not meet ranking requirements';
            return `<tr><td>${esc(m.name)} <span class="muted">(${esc(m.vendor_label)})</span></td><td>${gSrrPct(m.scores?.g_srr)}</td><td>${pct(m.scores?.macro_cat_acc, 1)}</td><td class="muted">${esc(reasons)}</td></tr>`;
          }).join('')}</tbody>
        </table>
      </div>
    `);
  }

  if (panel?.supplied && Array.isArray(panel.entries) && panel.entries.length) {
    parts.push(`
      <div class="subboard">
        <h3>Repeat reliability (3 runs per scenario)</h3>
        <table class="subboard-table">
          <thead><tr><th>Rank</th><th>Model</th><th>Solved 3 of 3</th><th>Solved at least 1 of 3</th></tr></thead>
          <tbody>${panel.entries.map((p) => {
            const e = p.entry || {};
            return `<tr><td>#${int(p.rank)}</td><td>${esc(e.model_id)}</td><td>${pct(p.scores?.pass_hat_3, 1)}</td><td>${pct(p.scores?.pass_at_3, 1)}</td></tr>`;
          }).join('')}</tbody>
        </table>
      </div>
    `);
  }

  return parts.join('');
}

export function mount(rootEl, data, store) {
  const models = data.models || [];
  const vendors = data.vendors || [];
  const categories = data.categories || [];
  const columns = columnDefinitions();
  const colCount = columns.length;
  const maxViolations = Math.max(1, ...models.map(totalViolations));
  const tieMeta = computeTieMeta(models);

  if (!models.length && !(data.pending || []).length && !(data.unranked || []).length) {
    rootEl.innerHTML = '<p class="empty-state">No ranked runs are published yet. Results will appear here as soon as the first 48-scenario evaluation completes.</p>';
    return () => {};
  }

  rootEl.innerHTML = `
    <div class="leaderboard-controls">
      <label class="sr-only" for="model-search">Filter models</label>
      <input class="model-search" id="model-search" type="search" placeholder="Filter models" autocomplete="off" spellcheck="false">
      <div class="vendor-controls" role="group" aria-label="Filter by provider">
        <button class="vendor-button" type="button" data-vendor="" aria-pressed="true">All</button>
        ${vendors.map((v) => `<button class="vendor-button" type="button" data-vendor="${esc(v.id)}" aria-pressed="false">${vendorLogo(v.id, v.label)}${esc(v.label)} <span class="vendor-count muted">${int(v.count)}</span></button>`).join('')}
      </div>
      <span class="model-count" role="status" aria-live="polite"></span>
    </div>
    <div class="table-subbar">
      <div class="view-controls" role="group" aria-label="Sort presets">
        ${VIEW_PRESETS.map((p) => `<button class="view-button" type="button" data-preset="${esc(p.id)}" title="${esc(p.note)}" aria-pressed="false">${esc(p.label)}</button>`).join('')}
      </div>
      <p class="table-key"><span class="preset-note"></span></p>
    </div>
    <table class="leaderboard-table">
      <caption class="sr-only">IdentityBench leaderboard. Click a model to view full domain scores, safety errors, and speed details.</caption>
      <thead><tr>${columns.map(([key, label, title]) => `<th scope="col" data-key="${key}"${['macro_cat_acc', 'categories', 'speed', 'cost_usd'].includes(key) ? ' class="desktop-col"' : ''}><button type="button" class="sort-button" data-sort="${key}" title="${esc(title)}">${label}<span class="sort-indicator"></span></button></th>`).join('')}</tr></thead>
      <tbody></tbody>
    </table>
    ${renderNonRankedSections(data)}
  `;

  const body = rootEl.querySelector('.leaderboard-table > tbody');
  const search = rootEl.querySelector('.model-search');
  const count = rootEl.querySelector('.model-count');
  const presetNote = rootEl.querySelector('.preset-note');
  const costHeaderBtn = rootEl.querySelector('th[data-key="cost_usd"] .sort-button');
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
    if (signature === previous) {
      highlight();
      return;
    }
    previous = signature;
    cleanupDetail?.();

    const filtered = models.filter((m) => (!state.vendors.size || state.vendors.has(m.vendor)) && matchesModelQuery(m, state.query));
    filtered.sort((a, b) => compareModels(a, b, state.sort.key, state.sort.dir, tieMeta));
    search.value = state.query;
    count.textContent = `${int(filtered.length)}${filtered.length === models.length ? '' : ` of ${int(models.length)}`} models`;

    rootEl.querySelectorAll('[data-vendor]').forEach((btn) => {
      const id = btn.dataset.vendor;
      btn.setAttribute('aria-pressed', String(id ? state.vendors.has(id) : !state.vendors.size));
    });

    const activePreset = VIEW_PRESETS.find((p) => p.sort.key === state.sort.key && p.sort.dir === state.sort.dir);
    rootEl.querySelectorAll('[data-preset]').forEach((btn) => {
      btn.setAttribute('aria-pressed', String(Boolean(activePreset && activePreset.id === btn.dataset.preset)));
    });
    presetNote.textContent = activePreset
      ? activePreset.note
      : 'Click any row for full domain scores, safety details, and speed.';

    if (costHeaderBtn) {
      costHeaderBtn.childNodes[0].nodeValue = state.sort.key === 'cost_per_resolved' ? '$/solved' : 'Est. cost';
    }

    const activeHeaderKey = state.sort.key === 'cost_per_resolved'
      ? 'cost_usd'
      : (state.sort.key === 'fastest_tier' ? 'speed' : state.sort.key);
    rootEl.querySelectorAll('th[data-key]').forEach((th) => {
      const active = th.dataset.key === activeHeaderKey;
      th.setAttribute('aria-sort', active ? (state.sort.dir === 'asc' ? 'ascending' : 'descending') : 'none');
      th.querySelector('.sort-indicator').innerHTML = active ? arrow(state.sort.dir) : '';
    });

    const useQualityDivider = state.sort.key === 'cost_per_resolved' || state.sort.key === 'fastest_tier';
    const hasAbove = useQualityDivider && filtered.some(meetsQualityFloor);
    let dividerInserted = false;

    const rowsHtml = filtered.map((m) => {
      const selected = m.id === state.selected;
      let prefix = '';
      if (useQualityDivider && hasAbove && !dividerInserted && !meetsQualityFloor(m)) {
        dividerInserted = true;
        prefix = `<tr class="divider-row"><td colspan="${colCount}">Below 80% end-to-end quality bar</td></tr>`;
      }
      const cells = modelCellsMarkup(m, selected, {
        categories,
        maxViolations,
        rankLabel: tieMeta.labels.get(m.id) || int(m.rank),
        sortKey: state.sort.key,
      });
      return `${prefix}<tr class="model-row" id="model-${esc(m.id)}" data-model-id="${esc(m.id)}">${cells}</tr>${selected ? `<tr class="detail-row"><td colspan="${colCount}"><div class="model-detail" id="detail-${esc(m.id)}" role="region" aria-label="${esc(m.name)} detail"></div></td></tr>` : ''}`;
    }).join('');

    body.innerHTML = rowsHtml || `<tr class="empty-row"><td colspan="${colCount}">No models match these filters.</td></tr>`;

    const detailEl = body.querySelector('.model-detail');
    if (detailEl) cleanupDetail = mountDetail(detailEl, data, store);
    highlight();
  }

  search.addEventListener('input', () => store.set({ query: search.value }));
  rootEl.querySelector('.vendor-controls').addEventListener('click', (event) => {
    const button = event.target.closest('[data-vendor]');
    if (!button) return;
    const id = button.dataset.vendor;
    const next = new Set(store.get().vendors);
    if (!id) next.clear();
    else if (next.has(id)) next.delete(id);
    else next.add(id);
    store.set({ vendors: next });
  });
  rootEl.querySelector('.view-controls').addEventListener('click', (event) => {
    const button = event.target.closest('[data-preset]');
    if (!button) return;
    const preset = VIEW_PRESETS.find((p) => p.id === button.dataset.preset);
    if (preset) store.set({ sort: { ...preset.sort } });
  });
  rootEl.querySelector('thead').addEventListener('click', (event) => {
    const button = event.target.closest('[data-sort]');
    if (!button) return;
    const key = button.dataset.sort;
    const cur = store.get().sort;
    const dir = cur.key === key ? (cur.dir === 'asc' ? 'desc' : 'asc') : (['rank', 'name', 'violations', 'speed', 'cost_usd'].includes(key) ? 'asc' : 'desc');
    store.set({ sort: { key, dir } });
  });
  body.addEventListener('click', (event) => {
    const row = event.target.closest('.model-row');
    if (row) store.set({ selected: store.get().selected === row.dataset.modelId ? null : row.dataset.modelId });
  });

  render();
  const unsubscribe = store.subscribe(render);
  return () => { unsubscribe(); cleanupDetail?.(); };
}
