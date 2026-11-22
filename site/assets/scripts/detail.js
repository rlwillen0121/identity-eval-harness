import { endToEndPct, pct, int, tokens, usd, elapsed, date, escapeHTML as esc } from './format.js';
import { vendorLogo } from './logos.js';

export const costBasis = {
  retail_api_estimate: 'Estimated: recorded tokens priced at retail API rates (the run used a vendor subscription CLI, so nothing was billed per token)',
  local: 'Local hardware; no run cost',
};

export function statusGlyph(pass) {
  return `<svg class="status-glyph ${pass ? 'pass' : 'fail'}" viewBox="0 0 16 16" aria-hidden="true"><path d="${pass ? 'M3 8l3 3 7-7' : 'M4 4l8 8M12 4l-8 8'}" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
}

const clamp = (n) => Math.min(1, Math.max(0, Number(n) || 0));

/** Every slot lands in exactly one outcome; shared by the detail panel and the outcomes chart. */
export const OUTCOME_SEGMENTS = [
  ['end_to_end', 'Resolved end to end', 'var(--seq-6)'],
  ['committed_failed', 'Committed, checks failed', 'var(--seq-4)'],
  ['committed_unsafe', 'Committed with violations', 'var(--seq-2)'],
  ['turn_limit', 'Turn limit', 'var(--seq-1)'],
  ['parse_failure', 'Parse failure', 'var(--critical)'],
];

const medianCache = new WeakMap();

function median(values) {
  const sorted = values.filter((value) => Number.isFinite(value)).sort((a, b) => a - b);
  if (!sorted.length) return 0;
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

const rateOf = (entry) => {
  const total = Number(entry?.planned_slots ?? entry?.slots) || 0;
  return total > 0 ? (Number(entry?.pass_count) || 0) / total : 0;
};

/** Field medians over ranked models only; pending runs never contribute. */
export function fieldMedians(data) {
  const models = Array.isArray(data.models) ? data.models : [];
  const categories = Object.fromEntries((data.categories || []).map((category) => [
    category.id,
    median(models.map((model) => rateOf(model.categories?.[category.id]))),
  ]));
  const subscores = Object.fromEntries((data.subscores || []).map((subscore) => [
    subscore.id,
    median(models.map((model) => rateOf(model.subscores?.[subscore.id]))),
  ]));
  const families = Object.fromEntries((data.families || []).map((family) => [
    family.id,
    median(models.map((model) => rateOf(model.families?.[family.id]))),
  ]));
  return { categories, subscores, families };
}

function cachedFieldMedians(data) {
  if (!medianCache.has(data)) medianCache.set(data, fieldMedians(data));
  return medianCache.get(data);
}

function bars(items) {
  return `<ul class="detail-bars">${items.map(({ label, hits, total, median: fieldMedian }) => {
    const rate = total ? hits / total : 0;
    const medianTick = Number.isFinite(fieldMedian)
      ? `<span class="field-median-tick" style="left:${clamp(fieldMedian) * 100}%" role="img" aria-label="Field median ${pct(fieldMedian)}" title="Field median ${pct(fieldMedian)}"></span>`
      : '';
    return `<li><div class="bar-label"><span>${esc(label)}</span><span>${int(hits)}/${int(total)} <span class="muted">${total ? pct(rate) : 'No attempts'}</span></span></div><div class="detail-bar-visual"><div class="detail-track" aria-hidden="true"><span class="detail-fill" style="width:${clamp(rate) * 100}%"></span></div>${medianTick}</div></li>`;
  }).join('')}</ul>`;
}

function figures(items, className = '') {
  return `<dl class="detail-figures ${className}">${items.map(([label, value]) => `<dt>${esc(label)}</dt><dd>${esc(value)}</dd>`).join('')}</dl>`;
}

const plural = (n, one, many) => `${int(n)} ${n === 1 ? one : many}`;

export function endToEndSummary(model, total) {
  return `${endToEndPct(model.g_srr)} (${int(model.g_srr_slots)}/${int(total)} · 95% interval ${endToEndPct(model.g_srr_ci[0])}–${endToEndPct(model.g_srr_ci[1])})`;
}

export function violationSummary(model, total) {
  return `${plural(model.unsafe_count, 'violation', 'violations')} in ${int(model.unsafe_slots)} of ${int(total)} engagements (${pct(model.unsafe_slot_rate)})`;
}

function formatCostDetail(model) {
  if (model.cost_basis === 'local') return costBasis.local;
  if (model.cost_usd == null) return 'Not recorded';
  return `${usd(model.cost_usd)} · ${costBasis[model.cost_basis] || 'Basis not recorded'}`;
}

function formatSourceKind(sourceKind) {
  if (sourceKind === 'native_rust_v51_run') return 'Native Rust v5.1 run';
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
    const total = Number(model.planned_slots) || Number(data.suite?.slots_per_run);
    const outcomes = model.outcomes || {};
    const families = Array.isArray(data.families) ? data.families : [];
    const regimes = Number(data.suite?.regimes) || 4;
    const perFamily = families.length ? Math.round(total / families.length) : 0;

    rootEl.innerHTML = `
      <div class="detail-heading"><h3>${vendorLogo(model.vendor, model.vendor_label, 'vendor-logo detail-logo')}${esc(model.name)} <span class="muted">· ${esc(model.vendor_label)} · ${esc(model.effort)} effort</span></h3><button type="button" class="text-button copy-link">Copy link</button></div>
      <p class="model-id">${esc(model.id)}</p>
      <p class="copy-status muted" role="status"></p>
      ${figures([
        ['End to end', endToEndSummary(model, total)],
        ['State accuracy', pct(model.state_accuracy)],
        ['Unsafe', violationSummary(model, total)],
        ['Total output tokens', `${tokens(model.output_tokens)} (${int(model.output_tokens)})`],
      ], 'mobile-figures')}
      <div class="detail-columns">
        <div>
          <h4>IGA categories</h4>
          <p class="detail-median-key muted"><span class="field-median-key" aria-hidden="true"></span> Field median across all ${int(data.models.length)} ranked models</p>
          ${bars((data.categories || []).map((category) => {
            const cat = model.categories?.[category.id] || { pass_count: 0, planned_slots: total };
            return {
              label: category.title || category.label,
              hits: cat.pass_count,
              total: cat.planned_slots || total,
              median: medians.categories[category.id],
            };
          }))}
          <h4>Subscores</h4>
          ${bars((data.subscores || []).map((subscore) => {
            const entry = model.subscores?.[subscore.id] || { pass_count: 0, slots: 0 };
            return { label: subscore.label, hits: entry.pass_count, total: entry.slots, median: medians.subscores[subscore.id] };
          }))}
          <h4>Resolution &amp; safety</h4>
          ${figures([
            ['End to end', endToEndSummary(model, total)],
            ['State accuracy', `${pct(model.state_accuracy)} of assertions satisfied, averaged over engagements`],
            ['Overgrants', `${int(model.overgrant_count)} in ${int(model.overgrant_slots)} of ${int(total)} engagements`],
            ['Availability breaches', `${int(model.avail_breach_count)} in ${int(model.avail_breach_slots)} of ${int(total)} engagements`],
            ['Any violation', `${int(model.unsafe_slots)} of ${int(total)} engagements (${pct(model.unsafe_slot_rate)})`],
            ['Committed', `${int(model.committed_slots)} of ${int(total)} engagements`],
            ['Turns per engagement', `${model.avg_turns} average (cap ${int(data.suite?.turn_budget)})`],
            ['Go-live dry runs', `${int(model.dry_runs_used)} used (up to ${int(data.suite?.max_dry_runs)} per engagement)`],
          ])}
        </div>
        <div>
          <h4>Engagement outcomes</h4>
          <div class="outcome-bar" role="img" aria-label="${esc(OUTCOME_SEGMENTS.map(([key, label]) => `${label}: ${int(outcomes[key] || 0)}`).join('; '))}">
            ${OUTCOME_SEGMENTS.filter(([key]) => (outcomes[key] || 0) > 0).map(([key, label, color]) => `<span style="flex:${outcomes[key]};background:${color}" title="${esc(label)}: ${int(outcomes[key])} of ${int(total)}"></span>`).join('')}
          </div>
          <ul class="outcome-legend">${OUTCOME_SEGMENTS.map(([key, label, color]) => `<li><span class="legend-swatch" style="background:${color}" aria-hidden="true"></span>${esc(label)} <strong>${int(outcomes[key] || 0)}</strong></li>`).join('')}</ul>
          <dl class="detail-terminations">
            <div><dt>Turn-limit stops</dt><dd>${int(outcomes.turn_limit || 0)}</dd></div>
            <div><dt>Parse failures</dt><dd>${int(outcomes.parse_failure || 0)}</dd></div>
            <div><dt>Provider errors</dt><dd>${int(outcomes.provider_error || 0)}</dd></div>
          </dl>
          <h4>Engagement families (F01–F12)</h4>
          <p class="detail-caption">End-to-end resolution across ${int(regimes)} client regimes (${int(perFamily)} engagements per family).</p>
          <div class="family-grid">${families.map((family) => {
            const entry = model.families?.[family.id] || { pass_count: 0, slots: 0 };
            const rate = rateOf(entry);
            const title = `${family.id} · ${family.label}: ${int(entry.pass_count)}/${int(entry.slots)} (${pct(rate)})`;
            return `<div class="family-cell" title="${esc(title)}"><span class="family-square" role="img" aria-label="${esc(title)}" style="background:var(--seq-${rate > 0 ? Math.max(1, Math.ceil(clamp(rate) * 7)) : 0})"></span><span>${esc(family.id)}</span></div>`;
          }).join('')}</div>
          <div class="scale-key"><span>0%</span>${Array.from({ length: 8 }, (_, i) => `<span style="background:var(--seq-${i})" aria-hidden="true"></span>`).join('')}<span>100%</span></div>
          ${figures([
            ['Output & reasoning tokens', `${tokens(model.output_tokens)} (${int(model.output_tokens)} total · ${tokens(model.reasoning_tokens)} reasoning)`],
            ['Input tokens', `${tokens(model.input_tokens)} uncached · ${tokens(model.cached_input_tokens)} cached`],
            ['Estimated cost', formatCostDetail(model)],
            ['Summed engagement time', elapsed(model.wall_time_ms)],
            ['Run setup', `Single agent · ${model.effort} effort · ${int(total)} engagements × ${int(data.suite?.repeats)} run`],
            ['Provenance', formatSourceKind(model.provenance?.source_kind)],
            ['Run summary SHA-256', model.provenance?.summary_sha256 || 'Not recorded'],
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
