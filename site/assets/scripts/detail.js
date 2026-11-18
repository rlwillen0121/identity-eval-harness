import { endToEndPct, pct, int, tokens, usd, seconds, date, escapeHTML as esc } from './format.js';
import { vendorLogo } from './logos.js';

export const costBasis = {
  retail_api: 'Retail token prices applied to the run\'s recorded tokens',
  billed: 'Amount billed for the run, when reported',
  local: 'Local hardware; no run cost',
};

export function statusGlyph(pass) {
  return `<svg class="status-glyph ${pass ? 'pass' : 'fail'}" viewBox="0 0 16 16" aria-hidden="true"><path d="${pass ? 'M3 8l3 3 7-7' : 'M4 4l8 8M12 4l-8 8'}" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
}

const clamp = (n) => Math.min(1, Math.max(0, Number(n) || 0));

const outcomeOrder = [
  ['end_to_end', 'End-to-end pass', 'var(--seq-6)'],
  ['state_only', 'State-correct only', 'var(--seq-4)'],
  ['completed_other', 'Completed (other fail)', 'var(--seq-2)'],
  ['turn_limit', 'Turn limit', 'var(--seq-1)'],
  ['output_budget', 'Output budget', 'var(--axis)'],
  ['parse_failure', 'Parse failure', 'var(--critical)'],
  ['context_limit', 'Context limit', 'var(--muted)'],
];

const medianCache = new WeakMap();

function median(values) {
  const sorted = values.filter((value) => Number.isFinite(value)).sort((a, b) => a - b);
  if (!sorted.length) return 0;
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

export function fieldMedians(data) {
  const models = Array.isArray(data.models) ? data.models : [];
  const slotsPerRun = Number(data.suite?.slots_per_run) || 144;
  const categoryFields = Object.fromEntries((data.categories || []).map((category) => [
    category.id,
    median(models.map((model) => {
      const cat = model.categories?.[category.id];
      const total = Number(cat?.planned_slots) || slotsPerRun;
      return total > 0 ? (Number(cat?.pass_count) || 0) / total : 0;
    })),
  ]));
  const familyFields = (data.families || []).map((_, index) => median(
    models.map((model) => Number(model.families?.[index]) || 0),
  ));
  return { categories: categoryFields, families: familyFields };
}

function cachedFieldMedians(data) {
  if (!medianCache.has(data)) medianCache.set(data, fieldMedians(data));
  return medianCache.get(data);
}

function bars(items) {
  return `<ul class="detail-bars">${items.map(({ label, meta, hits, total, median: fieldMedian }) => {
    const rate = total ? hits / total : 0;
    const medianTick = Number.isFinite(fieldMedian)
      ? `<span class="field-median-tick" style="left:${clamp(fieldMedian) * 100}%" role="img" aria-label="Field median ${pct(fieldMedian)}" title="Field median ${pct(fieldMedian)}"></span>`
      : '';
    return `<li><div class="bar-label"><span>${esc(label)}${meta ? ` <span class="detail-phase">${esc(meta)}</span>` : ''}</span><span>${int(hits)}/${int(total)} <span class="muted">${total ? pct(rate) : 'No attempts'}</span></span></div><div class="detail-bar-visual"><div class="detail-track" aria-hidden="true"><span class="detail-fill" style="width:${clamp(rate) * 100}%"></span></div>${medianTick}</div></li>`;
  }).join('')}</ul>`;
}

function figures(items, className = '') {
  return `<dl class="detail-figures ${className}">${items.map(([label, value]) => `<dt>${esc(label)}</dt><dd>${esc(value)}</dd>`).join('')}</dl>`;
}

export function stateAccuracySummary(model, total) {
  return `${int(model.sem_srr_slots)} of ${int(total)} slots (${pct(model.sem_srr_commit)})`;
}

function formatCostDetail(model) {
  if (model.cost_basis === 'local') {
    const est = model.recorded_estimated_cost_usd != null
      ? ` (reference token estimate ${usd(model.recorded_estimated_cost_usd)})`
      : '';
    return `${costBasis['local']}${est}`;
  }
  const coverage = model.accounting_coverage && model.accounting_coverage !== 'COMPLETE'
    ? ` · ${model.accounting_coverage.toLowerCase()} accounting`
    : '';
  return `${usd(model.cost_usd)} · ${costBasis[model.cost_basis] || 'Basis not recorded'}${coverage}`;
}

function formatSourceKind(sourceKind) {
  if (sourceKind === 'native_rust_run') return 'Native Rust v5 run';
  if (sourceKind === 'historical_native_audit') return 'Historical trajectory regraded by native Rust v5';
  return sourceKind || 'Not recorded';
}

export function mount(rootEl, data, store) {
  const medians = cachedFieldMedians(data);
  let previous;
  function render() {
    const id = store.get().selected;
    if (id === previous) return;
    previous = id;
    const model = data.models.find((m) => m.id === id);
    if (!model) { rootEl.replaceChildren(); return; }
    const total = Number(model.planned_slots) || Number(data.suite?.slots_per_run) || 144;
    const failures = model.failures || {};
    const outcomes = model.outcomes || {};
    const families = Array.isArray(data.families) ? data.families : [];

    rootEl.innerHTML = `
      <div class="detail-heading"><h3>${vendorLogo(model.vendor, model.vendor_label, 'vendor-logo detail-logo')}${esc(model.name)} <span class="muted">· ${esc(model.vendor_label)} · ${esc(model.effort)} effort</span></h3><button type="button" class="text-button copy-link">Copy link</button></div>
      <p class="model-id">${esc(model.id)}</p>
      <p class="copy-status muted" role="status"></p>
      ${figures([
        ['End to end (G-SRR)', `${endToEndPct(model.g_srr)} (${int(model.g_srr_slots)}/${int(total)} · 95% CI ${endToEndPct(model.g_srr_ci[0])}–${endToEndPct(model.g_srr_ci[1])})`],
        ['State accuracy (Sem-SRR@commit)', `${pct(model.sem_srr_commit)} (${int(model.sem_srr_slots)}/${int(total)})`],
        ['Safety (OG / AB)', `${pct(model.overgrant_rate)} OG (${int(model.overgrant_count)}) · ${pct(model.avail_breach_rate)} AB (${int(model.avail_breach_count)})`],
        ['Total output tokens', `${tokens(model.output_tokens)} (${int(model.output_tokens)})`],
      ], 'mobile-figures')}
      <div class="detail-columns">
        <div>
          <h4>7 Governance categories</h4>
          <p class="detail-median-key muted"><span class="field-median-key" aria-hidden="true"></span> Field median across all ${int(data.models.length)} models</p>
          ${bars((data.categories || []).map((category) => {
            const cat = model.categories?.[category.id] || { pass_count: 0, planned_slots: total };
            return {
              label: category.label,
              meta: (category.phases || []).join('+'),
              hits: cat.pass_count,
              total: cat.planned_slots || total,
              median: medians.categories[category.id],
            };
          }))}
          <h4>Resolution &amp; safety metrics</h4>
          ${figures([
            ['G-SRR (strict 2-phase)', `${endToEndPct(model.g_srr)} · 95% CI ${endToEndPct(model.g_srr_ci[0])}–${endToEndPct(model.g_srr_ci[1])}`],
            ['Sem-SRR@commit', `${pct(model.sem_srr_commit)} · 95% CI ${pct(model.sem_srr_ci[0])}–${pct(model.sem_srr_ci[1])}`],
            ['G-PROP (Phase 1 -> 2)', `${pct(model.g_prop)} (reset ${pct(model.g_prop_reset)})`],
            ['OVERGRANT@commit', `${pct(model.overgrant_rate)} mean (${int(model.overgrant_count)} committed)`],
            ['AVAIL_BREACH@commit', `${pct(model.avail_breach_rate)} mean (${int(model.avail_breach_count)} committed)`],
            ['Execution-valid rate', `${pct(model.execution_valid_rate)} (${int(model.observed_slots)}/${int(total)} observed)`],
          ])}
        </div>
        <div>
          <h4>144-slot outcomes &amp; distinct terminations</h4>
          <div class="outcome-bar" role="img" aria-label="${esc(outcomeOrder.map(([key, label]) => `${label}: ${int(outcomes[key] || 0)}`).join('; '))}">
            ${outcomeOrder.filter(([key]) => (outcomes[key] || 0) > 0).map(([key, label, color]) => `<span style="flex:${outcomes[key]};background:${color}" title="${esc(label)}: ${int(outcomes[key])} of ${int(total)}"></span>`).join('')}
          </div>
          <ul class="outcome-legend">${outcomeOrder.map(([key, label, color]) => `<li><span class="legend-swatch" style="background:${color}" aria-hidden="true"></span>${esc(label)} <strong>${int(outcomes[key] || 0)}</strong></li>`).join('')}</ul>
          <dl class="detail-terminations">
            <div><dt>Parse failures</dt><dd>${int(failures.parse_failures || 0)}</dd></div>
            <div><dt>Turn limits</dt><dd>${int(failures.turn_limits || 0)}</dd></div>
            <div><dt>Output-budget stops</dt><dd>${int(failures.output_limits || 0)}</dd></div>
            <div><dt>Context-limit stops</dt><dd>${int(failures.context_limits || 0)}</dd></div>
          </dl>
          <h4>12 Coupling families (F01–F12)</h4>
          <p class="detail-caption">Mean G-SRR across 4 compound policy regimes × 3 repeats (12 slots per family).</p>
          <div class="family-grid">${(model.families || []).map((rate, i) => {
            const family = families[i] || { id: `F${String(i + 1).padStart(2, '0')}`, label: '' };
            const title = `${family.id}${family.label ? ` · ${family.label}` : ''}: ${pct(rate)}`;
            return `<div class="family-cell" title="${esc(title)}"><span class="family-square" role="img" aria-label="${esc(title)}" style="background:var(--seq-${rate > 0 ? Math.max(1, Math.ceil(clamp(rate) * 7)) : 0})"></span><span>${esc(family.id)}</span></div>`;
          }).join('')}</div>
          <div class="scale-key"><span>0%</span>${Array.from({ length: 8 }, (_, i) => `<span style="background:var(--seq-${i})" aria-hidden="true"></span>`).join('')}<span>100%</span></div>
          ${figures([
            ['State accuracy', stateAccuracySummary(model, total)],
            ['Output & reasoning tokens', `${tokens(model.output_tokens)} (${int(model.output_tokens)} total · ${tokens(model.reasoning_tokens)} reasoning)`],
            ['Input tokens', `${tokens(model.input_tokens)} (${tokens(model.cached_input_tokens)} cached)`],
            ['Run cost', formatCostDetail(model)],
            ['Elapsed wall time', seconds((Number(model.wall_time_ms) || 0) / 1000)],
            ['Protocol & topology', `${model.provenance?.topology || 'T0_SINGLE_MONOLITH'} · ${model.provenance?.steering_mode || 'UNSTEERED'} · ${model.provenance?.wave_execution_mode || 'COUPLED'}`],
            ['Provenance', formatSourceKind(model.provenance?.source_kind)],
            ['Run manifest SHA-256', model.provenance?.run_manifest_sha256 || 'Not recorded'],
            ['First published', date(model.first_published)],
          ])}
        </div>
      </div>`;

    rootEl.querySelector('.copy-link').addEventListener('click', async () => {
      const url = new URL(window.location.href);
      url.hash = `m=${encodeURIComponent(model.id)}`;
      const status = rootEl.querySelector('.copy-status');
      try {
        await navigator.clipboard.writeText(url.href);
        status.textContent = 'Link copied.';
      } catch {
        status.textContent = 'Copy this link:';
        let input = rootEl.querySelector('.copy-fallback');
        if (!input) {
          input = document.createElement('input');
          input.className = 'copy-fallback';
          input.readOnly = true;
          input.setAttribute('aria-label', 'Model link to copy');
          status.after(input);
        }
        input.value = url.href;
        input.focus();
        input.select();
      }
    });
  }
  render();
  return store.subscribe(render);
}
