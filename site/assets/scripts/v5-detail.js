import { endToEndPct, pct, int, tokens, usd, seconds, escapeHTML as esc } from './format.js';
import { costBasis } from './detail.js';
import { vendorLogo } from './logos.js';

const clamp = (n) => Math.min(1, Math.max(0, Number(n) || 0));
const medianCache = new WeakMap();

function median(values) {
  const sorted = values.filter((v) => Number.isFinite(v)).sort((a, b) => a - b);
  if (!sorted.length) return 0;
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

export function v5FieldMedians(data) {
  const models = Array.isArray(data.models) ? data.models : [];
  return Object.fromEntries((data.categories || []).map((cat) => [
    cat.id,
    median(models.map((m) => Number(m.categories?.[cat.id]?.rate) || 0)),
  ]));
}

function cachedMedians(data) {
  if (!medianCache.has(data)) medianCache.set(data, v5FieldMedians(data));
  return medianCache.get(data);
}

const outcomeSpec = [
  ['g_srr_pass', 'G-SRR pass', 'var(--seq-6)'],
  ['completed_non_pass', 'Completed (non-pass)', 'var(--seq-3)'],
  ['turn_limits', 'Turn limit', 'var(--seq-2)'],
  ['output_limits', 'Output budget', 'var(--seq-1)'],
  ['parse_failures', 'Parse failure', 'var(--critical)'],
  ['context_limits', 'Context limit', 'var(--muted)'],
];

function formatCostDetail(model) {
  if (model.cost_basis === 'local') {
    const ref = Number.isFinite(model.recorded_estimated_cost_usd)
      ? ` (audit reference token estimate ${usd(model.recorded_estimated_cost_usd)})`
      : '';
    return `No run cost (local hardware)${ref}`;
  }
  const basisLabel = costBasis[model.cost_basis] || 'Basis not recorded';
  const coverage = model.accounting_coverage && model.accounting_coverage !== 'COMPLETE'
    ? ` · ${model.accounting_coverage} accounting coverage`
    : '';
  return `${usd(model.cost_usd)} · ${basisLabel}${coverage}`;
}

export function mount(rootEl, data, store) {
  const medians = cachedMedians(data);
  let previous;
  function render() {
    const id = store.get().selected;
    if (id === previous) return;
    previous = id;
    const model = data.models.find((m) => m.id === id);
    if (!model) { rootEl.replaceChildren(); return; }
    const total = model.planned_slots;
    const outcomes = outcomeSpec.map(([key, label, color]) => [key, label, color, Number(model.outcomes?.[key]) || 0]);

    rootEl.innerHTML = `
      <div class="detail-heading"><h3>${vendorLogo(model.vendor, model.vendor_label, 'vendor-logo detail-logo')}${esc(model.name)} <span class="muted">· ${esc(model.vendor_label)} · ${esc(model.effort)} effort · ${esc(model.tier)}</span></h3><button type="button" class="text-button copy-link">Copy link</button></div>
      <p class="model-id">${esc(model.id)} · ${esc(model.run_id)}</p>
      <p class="copy-status muted" role="status"></p>
      <div class="detail-columns">
        <div>
          <h4>7 Category pass@1</h4>
          <p class="detail-median-key muted"><span class="field-median-key" aria-hidden="true"></span> Field median across ${int(data.models.length)} complete runs</p>
          <ul class="detail-bars">${data.categories.map((cat) => {
            const entry = model.categories?.[cat.id] || { pass_count: 0, planned_slots: total, rate: 0 };
            const rate = Number(entry.rate) || 0;
            const fieldMed = medians[cat.id];
            const medianTick = Number.isFinite(fieldMed)
              ? `<span class="field-median-tick" style="left:${clamp(fieldMed) * 100}%" role="img" aria-label="Field median ${pct(fieldMed)}" title="Field median ${pct(fieldMed)}"></span>`
              : '';
            return `<li><div class="bar-label"><span>${esc(cat.label)} <span class="muted">${esc((cat.mapped_phases || []).join('+'))}</span></span><span>${int(entry.pass_count)}/${int(entry.planned_slots)} <span class="muted">${pct(rate)}</span></span></div><div class="detail-bar-visual"><div class="detail-track" aria-hidden="true"><span class="detail-fill" style="width:${clamp(rate) * 100}%"></span></div>${medianTick}</div></li>`;
          }).join('')}</ul>
          <h4>Protocol &amp; safety metrics</h4>
          <dl class="v5-detail-grid">
            <div><dt>G-SRR (95% CI)</dt><dd>${endToEndPct(model.g_srr)} (${endToEndPct(model.g_srr_ci[0])}–${endToEndPct(model.g_srr_ci[1])})</dd></div>
            <div><dt>Sem-SRR@commit (95% CI)</dt><dd>${pct(model.sem_srr_commit)} (${pct(model.sem_srr_ci[0])}–${pct(model.sem_srr_ci[1])})</dd></div>
            <div><dt>Action-SRR@last_stage</dt><dd>${pct(model.action_srr_last_stage)}</dd></div>
            <div><dt>G-PROP / G-PROP_reset</dt><dd>${pct(model.g_prop)} / ${pct(model.g_prop_reset)}</dd></div>
            <div><dt>OVERGRANT@commit</dt><dd>${pct(model.overgrant_rate)} (${int(model.overgrant_count)} slots)</dd></div>
            <div><dt>AVAIL_BREACH@commit</dt><dd>${pct(model.avail_breach_rate)} (${int(model.avail_breach_count)} slots)</dd></div>
          </dl>
        </div>
        <div>
          <h4>144-slot outcomes &amp; distinct terminations</h4>
          <div class="outcome-bar" role="img" aria-label="${esc(outcomes.map(([, label, , n]) => `${label}: ${int(n)}`).join('; '))}">
            ${outcomes.filter(([, , , n]) => n > 0).map(([, label, color, n]) => `<span style="flex:${n};background:${color}" title="${esc(label)}: ${int(n)} of ${int(total)}"></span>`).join('')}
          </div>
          <ul class="outcome-legend">${outcomes.map(([, label, color, n]) => `<li><span class="legend-swatch" style="background:${color}" aria-hidden="true"></span>${esc(label)} <strong>${int(n)}</strong></li>`).join('')}</ul>
          <h4>By scenario family (F01–F12)</h4>
          <p class="detail-caption">Mean G-SRR across the 4 regimes (12 slots per family).</p>
          <div class="family-grid">${model.families.map((rate, i) => {
            const family = `F${String(i + 1).padStart(2, '0')}`;
            return `<div class="family-cell" title="${family} ${pct(rate)}"><span class="family-square" role="img" aria-label="${family} ${pct(rate)}" style="background:var(--seq-${rate > 0 ? Math.max(1, Math.ceil(clamp(rate) * 7)) : 0})"></span><span>${family}</span></div>`;
          }).join('')}</div>
          <div class="scale-key"><span>0%</span>${Array.from({ length: 8 }, (_, i) => `<span style="background:var(--seq-${i})" aria-hidden="true"></span>`).join('')}<span>100%</span></div>
          <dl class="detail-figures">
            <dt>Execution-valid rate</dt><dd>${pct(model.execution_valid_rate)} (${int(model.observed_slots)}/${int(model.planned_slots)} observed)</dd>
            <dt>Provider / Parse / Turn</dt><dd>${int(model.failures.provider_errors)} provider · ${int(model.failures.parse_failures)} parse · ${int(model.failures.turn_limits)} turn limit</dd>
            <dt>Input / Output tokens</dt><dd>${tokens(model.input_tokens)} in · ${tokens(model.output_tokens)} out (${tokens(model.reasoning_tokens)} reasoning)</dd>
            <dt>Run cost</dt><dd>${esc(formatCostDetail(model))}</dd>
            <dt>Wall time (all 144 slots)</dt><dd>${seconds(model.wall_time_ms / 1000)}</dd>
            <dt>Gates (ECON / TURN / CAL-03)</dt><dd>${esc(model.gate_status['G-ECON-01'] || 'UNMEASURED')} / ${esc(model.gate_status['G-TURN-01'] || 'UNMEASURED')} / ${esc(model.gate_status['G-CAL-03'] || 'UNMEASURED')}</dd>
          </dl>
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
