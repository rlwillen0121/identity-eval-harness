import { gSrrPct, pct, int, num, usd, compact, seconds, duration, tokPerSec, formatInterval, date, escapeHTML as esc } from './format.js';
import { vendorLogo } from './logos.js';

const clamp = (n) => Math.min(1, Math.max(0, Number(n) || 0));

const REGIME_SHORT = {
  R1: 'Zero-Trust',
  R2: 'Multi-Affil.',
  R3: 'Hybrid Cloud',
  R4: 'M&A',
};

function bars(items) {
  return `<ul class="detail-bars">${items.map(({ label, rate }) => {
    const valid = typeof rate === 'number' && Number.isFinite(rate);
    return `<li><div class="bar-label"><span>${esc(label)}</span><span>${valid ? pct(rate, 1) : 'n/a'}</span></div><div class="detail-track" aria-hidden="true"><span class="detail-fill" style="width:${valid ? clamp(rate) * 100 : 0}%"></span></div></li>`;
  }).join('')}</ul>`;
}

function figures(items) {
  return `<dl class="detail-figures">${items.map(([label, value]) => `<dt>${esc(label)}</dt><dd>${esc(value)}</dd>`).join('')}</dl>`;
}

function renderTaxonomy(taxonomy, categoryMap) {
  if (!taxonomy) return '<p class="muted">No recurring failures recorded.</p>';
  const checks = [];
  for (const [catId, catData] of Object.entries(taxonomy.by_category || {})) {
    const domainName = categoryMap.get(catId) || catId;
    for (const item of catData.check_counts || []) {
      checks.push({ domainName, check: item.check_name, count: item.count });
    }
  }
  checks.sort((a, b) => b.count - a.count || a.check.localeCompare(b.check));
  const top = checks.slice(0, 10);
  const suppressed = Number(taxonomy.suppressed_cells) || 0;
  return `
    ${top.length
      ? `<ul class="taxonomy-list">${top.map((t) => `<li><span><strong>${esc(t.domainName)}</strong> · ${esc(t.check)}</span><strong>${int(t.count)}</strong></li>`).join('')}</ul>`
      : '<p class="muted">Zero recurring check failures (3 or more scenarios).</p>'}
    <p class="muted detail-footnote">Rare failures (&lt;3 scenarios) are withheld while this suite is active (${int(suppressed)} withheld).</p>
  `;
}

function verdictSentence(model) {
  const s = model.scores || {};
  const d = model.diagnostics || {};
  const u = model.usage || {};
  const og = Number(s.og_total) || 0;
  const ab = Number(s.ab_total) || 0;
  const safetyPhrase = og === 0 && ab === 0
    ? 'zero safety errors'
    : `${int(og)} overgrant${og === 1 ? '' : 's'} and ${int(ab)} broken access incident${ab === 1 ? '' : 's'}`;
  return `Solved ${int(s.resolved_slots)} of ${int(s.planned_slots)} scenarios with ${safetyPhrase}, taking ${duration(d.median_slot_wall_time_s)} median per scenario at ${usd(u.estimated_cost_usd)} estimated cost.`;
}

export function mount(rootEl, data, store) {
  let previous;
  const categoryMap = new Map((data.categories || []).map((c) => [c.id, c.label || c.title]));

  function render() {
    const id = store.get().selected;
    if (id === previous) return;
    previous = id;
    const model = (data.models || []).find((m) => m.id === id);
    if (!model) {
      rootEl.replaceChildren();
      return;
    }
    const s = model.scores || {};
    const d = model.diagnostics || {};
    const e = model.efficiency || {};
    const u = model.usage || {};
    const meta = model.metadata || {};
    const catRates = s.category_pass_rates || {};
    const famMarg = model.marginals?.family_g_srr || {};
    const regMarg = model.marginals?.regime_g_srr || {};
    const effortPart = model.reasoning_effort && model.reasoning_effort !== 'transport_default'
      ? ` · ${model.reasoning_effort}`
      : '';

    rootEl.innerHTML = `
      <div class="detail-heading">
        <h3>${vendorLogo(model.vendor, model.vendor_label, 'vendor-logo detail-logo')}${esc(model.name)} <span class="muted">· ${esc(model.vendor_label)}${esc(effortPart)}</span></h3>
        <div class="detail-actions">
          <button type="button" class="text-button copy-link">Copy link</button>
          <button type="button" class="text-button close-detail" aria-label="Close details">Close</button>
        </div>
      </div>
      <p class="detail-verdict">${esc(verdictSentence(model))}</p>
      <p class="model-id">End to end ${esc(gSrrPct(s.g_srr))} (95% range ${esc(formatInterval(s.g_srr_interval, true))} · rank range ${esc(formatInterval(s.rank_interval, false))})</p>
      <p class="copy-status muted" role="status"></p>
      <div class="detail-columns">
        <div>
          <h4>Scores across 8 IAM domains</h4>
          ${bars((data.categories || []).map((c) => ({ label: c.title, rate: catRates[c.id] })))}
          <h4>Key governance tracks</h4>
          ${figures([
            ['Machine identity custody', pct(s.subscores?.nhi_custody_pass_rate, 1)],
            ['AI agent delegation (RFC 8693)', pct(s.subscores?.agent_delegation_pass_rate, 1)],
            ['SailPoint IIQ to ISC migration', `${pct(s.iiq_to_isc_track?.pass_rate, 1)} (${int(s.iiq_to_isc_track?.applicable_slots)} scenarios)`],
            ['Domain average', pct(s.macro_cat_acc, 1)],
            ['Completion rate', pct(s.commit_rate, 1)],
            ['Overgrants', int(s.og_total)],
            ['Broken access', int(s.ab_total)],
          ])}
          <h4>Most common failures</h4>
          ${renderTaxonomy(model.failure_taxonomy, categoryMap)}
        </div>
        <div>
          <h4>By scenario type (12 enterprise patterns)</h4>
          <div class="family-grid">${(data.families || []).map((f) => {
            const rate = famMarg[f.id];
            const valid = typeof rate === 'number' && Number.isFinite(rate);
            const bucket = valid && rate > 0 ? Math.max(1, Math.ceil(clamp(rate) * 7)) : 0;
            const lightText = bucket >= 4 ? ' is-dark-cell' : '';
            return `<div class="family-cell" title="${esc(f.label)} (${valid ? pct(rate, 0) : 'n/a'})"><span class="family-square${lightText}" style="background:var(--seq-${bucket})">${valid ? pct(rate, 0) : 'n/a'}</span><span class="family-name">${esc(f.label.split(' ').slice(0, 2).join(' '))}</span></div>`;
          }).join('')}</div>
          <h4>By enterprise environment</h4>
          <div class="regime-grid">${(data.regimes || []).map((r) => `<div class="regime-chip" title="${esc(r.label)}"><div>${esc(REGIME_SHORT[r.id] || r.id)}</div><strong>${pct(regMarg[r.id], 0)}</strong></div>`).join('')}</div>
          <h4>Speed &amp; estimated cost</h4>
          ${figures([
            ['Estimated total cost', usd(u.estimated_cost_usd)],
            ['Estimated cost per solved', usd(u.cost_per_resolved_slot_usd)],
            ['Median time per scenario', duration(d.median_slot_wall_time_s)],
            ['Average turns per scenario', num(d.mean_turns, 1)],
            ['Median turns when solved', num(e.median_turns_per_resolved_slot, 1)],
            ['Generation speed', tokPerSec(d.generation_tokens_per_second)],
            ['First published', model.first_published ? date(model.first_published) : 'Unpublished'],
          ])}
          <details class="tech-details">
            <summary>Technical details and audit hashes</summary>
            ${figures([
              ['Model ID', model.model_id || 'n/a'],
              ['Transport', model.transport_label || 'n/a'],
              ['State accuracy', pct(s.state_acc, 1)],
              ['Turn latency (median / p95)', `${seconds(d.median_turn_latency_s)} / ${seconds(d.p95_turn_latency_s)}`],
              ['Uncached / cached input', `${compact(u.uncached_input_tokens)} / ${compact(u.cache_read_tokens)}`],
              ['Cache write tokens', compact(u.cache_write_tokens)],
              ['Output / thinking tokens', `${compact(u.output_tokens)} / ${compact(u.reasoning_tokens)}`],
              ['Format retries', int(d.format_error_turns)],
              ['Pricing SHA-256', (meta.pricing_sha256 || 'n/a').slice(0, 16)],
              ['Probe SHA-256', (meta.receipt_sha256 || 'n/a').slice(0, 16)],
              ['Archive SHA-256', (meta.withheld_bundle_sha256 || 'n/a').slice(0, 16)],
            ])}
          </details>
        </div>
      </div>
    `;

    rootEl.querySelector('.copy-link').addEventListener('click', async () => {
      const url = new URL(window.location.href);
      url.hash = `m=${encodeURIComponent(model.id)}`;
      const status = rootEl.querySelector('.copy-status');
      try {
        await navigator.clipboard.writeText(url.href);
        status.textContent = 'Link copied.';
      } catch {
        status.textContent = `Link: ${url.href}`;
      }
    });
    rootEl.querySelector('.close-detail').addEventListener('click', () => {
      store.set({ selected: null });
    });
  }
  render();
  return store.subscribe(render);
}
