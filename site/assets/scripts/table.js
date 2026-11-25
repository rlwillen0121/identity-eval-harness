import { V52_INTERVAL_METHOD, gSrrPct, pct, int, num, usd, formatInterval, tokPerSec, escapeHTML as esc } from './format.js';
import { mount as mountDetail } from './detail.js';
import { vendorLogo } from './logos.js';

const arrow = (dir) => `<svg class="sort-arrow" viewBox="0 0 10 12" aria-hidden="true"><path d="${dir === 'asc' ? 'M5 10V2M2 5l3-3 3 3' : 'M5 2v8M2 7l3 3 3-3'}" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>`;
const checkGlyph = () => '<svg class="clean-check" viewBox="0 0 12 12" aria-hidden="true"><path d="M2.2 6.3 4.8 8.9 9.8 3.3" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const clamp = (n) => Math.min(1, Math.max(0, Number(n) || 0));

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

function tieSignature(model) {
  const s = model.scores || {};
  return [
    Number(s.g_srr ?? -1).toFixed(6),
    Number(s.macro_cat_acc ?? -1).toFixed(6),
    Number(s.commit_rate ?? -1).toFixed(6),
    Number(s.violations_per_committed_slot ?? -1).toFixed(6),
  ].join('|');
}

function computeTieGroups(models) {
  const counts = new Map();
  const minRank = new Map();
  for (const m of models) {
    const sig = tieSignature(m);
    counts.set(sig, (counts.get(sig) || 0) + 1);
    const r = Number(m.rank) || 0;
    if (!minRank.has(sig) || r < minRank.get(sig)) minRank.set(sig, r);
  }
  const labels = new Map();
  for (const m of models) {
    const sig = tieSignature(m);
    labels.set(m.id, counts.get(sig) > 1 ? `T${minRank.get(sig)}` : int(m.rank));
  }
  return labels;
}

function sortValue(model, key) {
  switch (key) {
    case 'rank': return Number(model.rank) || 0;
    case 'name': return model.name || '';
    case 'g_srr': return Number(model.scores?.g_srr) || 0;
    case 'macro_cat_acc': return Number(model.scores?.macro_cat_acc) || 0;
    case 'categories': return categoryMean(model);
    case 'violations': return totalViolations(model);
    case 'cost_usd': return Number(model.usage?.estimated_cost_usd) || 0;
    case 'cost_per_resolved': {
      const v = Number(model.usage?.cost_per_resolved_slot_usd);
      return Number.isFinite(v) && v > 0 ? v : Number.POSITIVE_INFINITY;
    }
    default: return Number(model.rank) || 0;
  }
}

export function compareModels(a, b, key, dir = 'asc') {
  const direction = dir === 'desc' ? -1 : 1;
  const av = sortValue(a, key);
  const bv = sortValue(b, key);
  const primary = typeof av === 'string' || typeof bv === 'string'
    ? String(av).localeCompare(String(bv), 'en')
    : av - bv;
  if (primary) return primary * direction;
  if (key === 'violations' || key === 'cost_per_resolved') {
    const gDiff = (Number(b.scores?.g_srr) || 0) - (Number(a.scores?.g_srr) || 0);
    if (gDiff) return gDiff;
  }
  return (Number(a.rank) || 0) - (Number(b.rank) || 0);
}

export const columnDefinitions = () => [
  ['rank', '#', 'Canonical rank under SPEC-v5.3 §9 (G-SRR → Macro-Cat-Acc → commit rate → violations/committed). Hover for 95% rank interval.'],
  ['name', 'Model', 'Model name, vendor, and reasoning effort. Green check marks zero overgrants and zero availability breaches.'],
  ['g_srr', 'End to end', `Strict end-to-end resolution across 48 scenarios (zero violations, every in-scope category passed) with 95% interval (${V52_INTERVAL_METHOD}).`],
  ['macro_cat_acc', 'Macro-Cat', 'Unweighted mean assertion accuracy across each scenario\'s in-scope IGA categories (SPEC-v5.3 companion score).'],
  ['categories', 'By category', 'Exact pass rate across each of the 8 enterprise IGA categories. Hover any bar for the category score.'],
  ['violations', 'OG / AB', 'Total overgrants (OG) and availability breaches (AB) across all 48 scenarios.'],
  ['cost_usd', 'Est. cost', 'Side metric only: estimated USD from recorded tokens under the shared pricing table. Never affects rank.'],
];

const VIEW_PRESETS = [
  { id: 'canonical', label: 'Canonical (G-SRR)', sort: { key: 'rank', dir: 'asc' }, title: 'SPEC-v5.3 §9 canonical ranking (G-SRR → Macro-Cat → commit → violations)' },
  { id: 'safety', label: 'Safety-first', sort: { key: 'violations', dir: 'asc' }, title: 'Sort by fewest OG + AB safety violations first, then highest G-SRR' },
  { id: 'value', label: 'Best value', sort: { key: 'cost_per_resolved', dir: 'asc' }, title: 'Sort by lowest estimated cost per resolved scenario (report-only)' },
];

function intervalGraphic(model) {
  const iv = model.scores?.g_srr_interval;
  if (!iv) return '';
  const lo = clamp(iv.lo) * 108;
  const hi = clamp(iv.hi) * 108;
  const x = clamp(model.scores?.g_srr) * 108;
  const label = `95% interval: ${pct(iv.lo, 1)} to ${pct(iv.hi, 1)}`;
  return `<svg class="ci-whisker" viewBox="0 0 108 16" role="img" aria-label="${esc(label)}" title="${esc(label)}"><line class="ci-baseline" x1="0" x2="108" y1="8" y2="8"/><line x1="${lo}" x2="${hi}" y1="8" y2="8"/><line x1="${lo}" x2="${lo}" y1="5" y2="11"/><line x1="${hi}" x2="${hi}" y1="5" y2="11"/><circle cx="${x}" cy="8" r="2.5"/></svg>`;
}

function categoryBarsMarkup(model, categories) {
  const rates = model.scores?.category_pass_rates || {};
  return `<div class="category-bars">${categories.map((cat) => {
    const rate = rates[cat.id];
    const valid = typeof rate === 'number' && Number.isFinite(rate);
    const fillPct = valid ? clamp(rate) * 100 : 0;
    const isFull = valid && rate >= 0.9999;
    const label = `${cat.title}: ${valid ? pct(rate, 0) : '—'}`;
    return `<span class="category-track" role="img" aria-label="${esc(label)}" title="${esc(label)}"><span class="category-fill${isFull ? ' is-full' : ''}" style="height:${fillPct}%"></span></span>`;
  }).join('')}</div>`;
}

function safetyCellMarkup(model, maxViolations) {
  const s = model.scores || {};
  const og = Number(s.og_total) || 0;
  const ab = Number(s.ab_total) || 0;
  const total = og + ab;
  if (total === 0) {
    return `<span class="safety-zero" title="Zero overgrants and zero availability breaches">${checkGlyph()}0 / 0</span>`;
  }
  const width = maxViolations > 0 ? Math.max(4, clamp(total / maxViolations) * 100) : 0;
  const severity = total >= 20 ? ' is-high' : ' is-warn';
  const title = `${int(og)} overgrants · ${int(ab)} availability breaches (${int(total)} total)`;
  return `<span class="unsafe-measure${severity}" title="${esc(title)}"><span class="unsafe-number">${int(og)} / ${int(ab)}</span><span class="unsafe-track" aria-hidden="true"><span class="unsafe-fill" style="width:${width}%"></span></span></span>`;
}

export function modelCellsMarkup(model, selected = false, options = {}) {
  const { categories = [], maxViolations = 1, rankLabel = int(model.rank) } = options;
  const s = model.scores || {};
  const d = model.diagnostics || {};
  const u = model.usage || {};
  const isClean = totalViolations(model) === 0;
  const rankTitle = s.rank_interval
    ? `Rank #${int(model.rank)} · 95% rank interval: ${formatInterval(s.rank_interval, false)}`
    : `Rank #${int(model.rank)}`;
  const costTitle = `Est. cost (report-only, never affects rank) · ${usd(u.cost_per_resolved_slot_usd)}/resolved · ${num(d.mean_turns, 1)} avg turns · ${tokPerSec(d.generation_tokens_per_second)}`;
  const effortText = model.reasoning_effort && model.reasoning_effort !== 'transport_default'
    ? ` · ${model.reasoning_effort}`
    : '';
  return `
    <td class="rank-cell" title="${esc(rankTitle)}">${esc(rankLabel)}</td>
    <td class="identity-cell">
      <button type="button" class="model-name" aria-expanded="${selected}"${selected ? ` aria-controls="detail-${esc(model.id)}"` : ''}>
        ${vendorLogo(model.vendor, model.vendor_label)}${esc(model.name)}
      </button>
      <div class="model-subline">
        <span>${esc(model.vendor_label)}${esc(effortText)}</span>
        ${isClean ? `<span class="screen-clear" role="img" aria-label="Zero overgrants and zero availability breaches" title="Zero overgrants and zero availability breaches">${checkGlyph()}</span>` : ''}
      </div>
    </td>
    <td class="resolution-cell"><span class="resolution-measure"><span class="resolution-number">${gSrrPct(s.g_srr)}</span><span class="resolution-count">${int(s.resolved_slots)}/${int(s.planned_slots)}</span>${intervalGraphic(model)}</span></td>
    <td class="desktop-col tasks-cell"><span class="tasks-measure"><span class="tasks-number">${pct(s.macro_cat_acc, 1)}</span><span class="tasks-track" aria-hidden="true"><span class="tasks-fill" style="width:${clamp(s.macro_cat_acc) * 100}%"></span></span></span></td>
    <td class="desktop-col">${categoryBarsMarkup(model, categories)}</td>
    <td class="desktop-col unsafe-cell">${safetyCellMarkup(model, maxViolations)}</td>
    <td class="desktop-col cost-cell" title="${esc(costTitle)}">${usd(u.estimated_cost_usd)}</td>
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
        <h3>In progress (unranked)</h3>
        <table class="subboard-table">
          <thead><tr><th>Entry</th><th>Observed</th><th>Observed G-SRR</th><th>Provisional bounds</th><th>Mean turns</th></tr></thead>
          <tbody>${pending.map((m) => {
            const b = m.scores?.provisional_g_srr_bounds;
            const boundsText = Array.isArray(b) ? `[${pct(b[0], 1)}, ${pct(b[1], 1)}]` : '—';
            return `<tr><td>${esc(m.name)} <span class="muted">(${esc(m.reasoning_effort)} · ${esc(m.transport_label)})</span></td><td>${int(m.scores?.observed_slots)}/${int(m.scores?.planned_slots)}</td><td>${pct(m.scores?.g_srr ?? null, 1)}</td><td>${boundsText}</td><td>${num(m.diagnostics?.mean_turns, 1)}</td></tr>`;
          }).join('')}</tbody>
        </table>
      </div>
    `);
  }

  if (unranked.length) {
    parts.push(`
      <div class="subboard">
        <h3>Complete but unranked (§10.1 eligibility)</h3>
        <table class="subboard-table">
          <thead><tr><th>Entry</th><th>Observed G-SRR</th><th>Macro-Cat</th><th>Eligibility reasons</th></tr></thead>
          <tbody>${unranked.map((m) => {
            const reasons = (m.eligibility?.reasons || []).map((r) => r.code || r.detail).join(', ') || 'ineligible';
            return `<tr><td>${esc(m.name)} <span class="muted">(${esc(m.reasoning_effort)} · ${esc(m.transport_label)})</span></td><td>${gSrrPct(m.scores?.g_srr)}</td><td>${pct(m.scores?.macro_cat_acc, 1)}</td><td class="muted">${esc(reasons)}</td></tr>`;
          }).join('')}</tbody>
        </table>
      </div>
    `);
  }

  if (panel?.supplied && Array.isArray(panel.entries) && panel.entries.length) {
    parts.push(`
      <div class="subboard">
        <h3>Reliability panel (k = 3)</h3>
        <table class="subboard-table">
          <thead><tr><th>Headline rank</th><th>Entry</th><th>pass^3</th><th>pass@3</th></tr></thead>
          <tbody>${panel.entries.map((p) => {
            const e = p.entry || {};
            return `<tr><td>#${int(p.rank)}</td><td>${esc(e.model_id)} (${esc(e.reasoning_effort)} · ${esc(e.transport_id)})</td><td>${pct(p.scores?.pass_hat_3, 1)}</td><td>${pct(p.scores?.pass_at_3, 1)}</td></tr>`;
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
  const maxViolations = Math.max(1, ...models.map(totalViolations));
  const rankLabels = computeTieGroups(models);

  if (!models.length && !(data.pending || []).length && !(data.unranked || []).length) {
    rootEl.innerHTML = '<p class="empty-state">No v5.3 ranked runs are imported yet. Once a <code>v53_core</code> run finishes, run <code>identitybench v53 leaderboard</code>, <code>node scripts/import-v53-run.mjs --leaderboard ...</code>, and <code>node site/build.mjs</code>.</p>';
    return () => {};
  }

  rootEl.innerHTML = `
    <div class="leaderboard-controls">
      <label class="sr-only" for="model-search">Filter models</label>
      <input class="model-search" id="model-search" type="search" placeholder="Filter models or transports" autocomplete="off" spellcheck="false">
      <div class="vendor-controls" role="group" aria-label="Filter by vendor">
        <button class="vendor-button" type="button" data-vendor="" aria-pressed="true">All</button>
        ${vendors.map((v) => `<button class="vendor-button" type="button" data-vendor="${esc(v.id)}" aria-pressed="false">${vendorLogo(v.id, v.label)}${esc(v.label)} <span class="vendor-count muted">${int(v.count)}</span></button>`).join('')}
      </div>
      <span class="model-count" role="status" aria-live="polite"></span>
    </div>
    <div class="table-subbar">
      <div class="view-controls" role="group" aria-label="Sort presets">
        ${VIEW_PRESETS.map((p) => `<button class="view-button" type="button" data-preset="${esc(p.id)}" title="${esc(p.title)}" aria-pressed="false">${esc(p.label)}</button>`).join('')}
      </div>
      <p class="table-key">
        <span class="screen-key">${checkGlyph()}Zero OG / AB</span>
        <span class="category-key">Categories: ${categories.map((c) => esc(c.short || c.label)).join(' · ')}</span>
      </p>
    </div>
    <table class="leaderboard-table">
      <caption class="sr-only">IdentityBench v5.3 ranked leaderboard. Activate a model name for full category, family, commit, and safety diagnostics.</caption>
      <thead><tr>${columns.map(([key, label, title]) => `<th scope="col" data-key="${key}"${['macro_cat_acc', 'categories', 'violations', 'cost_usd'].includes(key) ? ' class="desktop-col"' : ''}><button type="button" class="sort-button" data-sort="${key}" title="${esc(title)}">${label}<span class="sort-indicator"></span></button></th>`).join('')}</tr></thead>
      <tbody></tbody>
    </table>
    ${renderNonRankedSections(data)}
  `;

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
    if (signature === previous) {
      highlight();
      return;
    }
    previous = signature;
    cleanupDetail?.();

    const filtered = models.filter((m) => (!state.vendors.size || state.vendors.has(m.vendor)) && matchesModelQuery(m, state.query));
    filtered.sort((a, b) => compareModels(a, b, state.sort.key, state.sort.dir));
    search.value = state.query;
    count.textContent = `${int(filtered.length)}${filtered.length === models.length ? '' : ` of ${int(models.length)}`} ranked`;

    rootEl.querySelectorAll('[data-vendor]').forEach((btn) => {
      const id = btn.dataset.vendor;
      btn.setAttribute('aria-pressed', String(id ? state.vendors.has(id) : !state.vendors.size));
    });
    rootEl.querySelectorAll('[data-preset]').forEach((btn) => {
      const preset = VIEW_PRESETS.find((p) => p.id === btn.dataset.preset);
      const active = Boolean(preset && preset.sort.key === state.sort.key && preset.sort.dir === state.sort.dir);
      btn.setAttribute('aria-pressed', String(active));
    });
    rootEl.querySelectorAll('th[data-key]').forEach((th) => {
      const active = th.dataset.key === state.sort.key;
      th.setAttribute('aria-sort', active ? (state.sort.dir === 'asc' ? 'ascending' : 'descending') : 'none');
      th.querySelector('.sort-indicator').innerHTML = active ? arrow(state.sort.dir) : '';
    });

    body.innerHTML = filtered.map((m) => {
      const selected = m.id === state.selected;
      const cells = modelCellsMarkup(m, selected, {
        categories,
        maxViolations,
        rankLabel: rankLabels.get(m.id) || int(m.rank),
      });
      return `<tr class="model-row" id="model-${esc(m.id)}" data-model-id="${esc(m.id)}">${cells}</tr>${selected ? `<tr class="detail-row"><td colspan="7"><div class="model-detail" id="detail-${esc(m.id)}" role="region" aria-label="${esc(m.name)} detail"></div></td></tr>` : ''}`;
    }).join('') || '<tr class="empty-row"><td colspan="7">No ranked models match these filters.</td></tr>';

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
    const dir = cur.key === key ? (cur.dir === 'asc' ? 'desc' : 'asc') : (['rank', 'name', 'violations', 'cost_usd'].includes(key) ? 'asc' : 'desc');
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
