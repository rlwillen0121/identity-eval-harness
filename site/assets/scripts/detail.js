import { gSrrPct, pct, int, num, usd, compact, seconds, tokPerSec, formatInterval, date, escapeHTML as esc } from './format.js';
import { vendorLogo } from './logos.js';

const clamp = (n) => Math.min(1, Math.max(0, Number(n) || 0));

function bars(items) {
  return `<ul class="detail-bars">${items.map(({ label, rate }) => {
    const valid = typeof rate === 'number' && Number.isFinite(rate);
    return `<li><div class="bar-label"><span>${esc(label)}</span><span>${valid ? pct(rate, 1) : '—'}</span></div><div class="detail-track" aria-hidden="true"><span class="detail-fill" style="width:${valid ? clamp(rate) * 100 : 0}%"></span></div></li>`;
  }).join('')}</ul>`;
}

function figures(items) {
  return `<dl class="detail-figures">${items.map(([label, value]) => `<dt>${esc(label)}</dt><dd>${esc(value)}</dd>`).join('')}</dl>`;
}

function renderTaxonomy(taxonomy) {
  if (!taxonomy) return '<p class="muted">No failure taxonomy recorded.</p>';
  const checks = [];
  for (const [catId, catData] of Object.entries(taxonomy.by_category || {})) {
    for (const item of catData.check_counts || []) {
      checks.push({ catId, check: item.check_name, count: item.count });
    }
  }
  checks.sort((a, b) => b.count - a.count || a.check.localeCompare(b.check));
  const top = checks.slice(0, 10);
  const suppressed = Number(taxonomy.suppressed_cells) || 0;
  const termEntries = Object.entries(taxonomy.termination_counts || {})
    .map(([k, v]) => `${k}: ${int(v)}`)
    .join(' · ');
  return `
    ${top.length
      ? `<ul class="taxonomy-list">${top.map((t) => `<li><span><code>${esc(t.catId)}</code> · ${esc(t.check)}</span><strong>${int(t.count)}</strong></li>`).join('')}</ul>`
      : '<p class="muted">Zero publishable check failures.</p>'}
    <p class="muted" style="font-size:11px;margin-top:8px;">Suppressed cells (&lt;3 scenarios): ${int(suppressed)}${termEntries ? ` · Terminations: ${esc(termEntries)}` : ''}</p>
  `;
}

export function mount(rootEl, data, store) {
  let previous;
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

    rootEl.innerHTML = `
      <div class="detail-heading">
        <h3>${vendorLogo(model.vendor, model.vendor_label, 'vendor-logo detail-logo')}${esc(model.name)} <span class="muted">· ${esc(model.vendor_label)} · ${esc(model.reasoning_effort)} · ${esc(model.transport_label)}</span></h3>
        <button type="button" class="text-button copy-link">Copy link</button>
      </div>
      <p class="model-id">${esc(model.id)} · G-SRR ${esc(gSrrPct(s.g_srr))} (95% CI ${esc(formatInterval(s.g_srr_interval, true))}, rank ${esc(formatInterval(s.rank_interval, false))})</p>
      <p class="copy-status muted" role="status"></p>
      <div class="detail-columns">
        <div>
          <h4>8 Modern Enterprise IGA Categories</h4>
          ${bars((data.categories || []).map((c) => ({ label: c.title, rate: catRates[c.id] })))}
          <h4>Subscores &amp; IIQ → ISC Track</h4>
          ${figures([
            ['NHI custody pass', pct(s.subscores?.nhi_custody_pass_rate, 1)],
            ['Agent delegation pass', pct(s.subscores?.agent_delegation_pass_rate, 1)],
            ['IIQ → ISC pass', `${pct(s.iiq_to_isc_track?.pass_rate, 1)} (${int(s.iiq_to_isc_track?.applicable_slots)} slots)`],
            ['Commit rate / OG / AB', `${pct(s.commit_rate, 1)} · ${int(s.og_total)} OG · ${int(s.ab_total)} AB`],
          ])}
          <h4>Failure Taxonomy (§10.2–§10.3)</h4>
          ${renderTaxonomy(model.failure_taxonomy)}
        </div>
        <div>
          <h4>By Scenario Family (F01–F12 G-SRR)</h4>
          <div class="family-grid">${(data.families || []).map((f) => {
            const rate = famMarg[f.id];
            const valid = typeof rate === 'number' && Number.isFinite(rate);
            const bucket = valid && rate > 0 ? Math.max(1, Math.ceil(clamp(rate) * 7)) : 0;
            return `<div class="family-cell" title="${esc(f.id)}: ${esc(f.label)} (${valid ? pct(rate, 0) : '—'})"><span class="family-square" style="background:var(--seq-${bucket})"></span><span>${esc(f.id)}</span></div>`;
          }).join('')}</div>
          <h4>By Organization Regime (R1–R4 G-SRR)</h4>
          <div class="regime-grid">${(data.regimes || []).map((r) => `<div class="regime-chip" title="${esc(r.label)}"><div>${esc(r.id)}</div><strong>${pct(regMarg[r.id], 0)}</strong></div>`).join('')}</div>
          <h4>Usage, Efficiency &amp; Timing Diagnostics</h4>
          ${figures([
            ['Uncached / Cache read / Write', `${compact(u.uncached_input_tokens)} / ${compact(u.cache_read_tokens)} / ${compact(u.cache_write_tokens)}`],
            ['Output / Reasoning tokens', `${compact(u.output_tokens)} / ${compact(u.reasoning_tokens)}`],
            ['Estimated cost / per resolved', `${usd(u.estimated_cost_usd)} / ${usd(u.cost_per_resolved_slot_usd)}`],
            ['Mean turns / resolved median', `${num(d.mean_turns, 1)} / ${num(e.median_turns_per_resolved_slot, 1)}`],
            ['Duplicate read / Format errors', `${pct(d.duplicate_read_rate, 1)} / ${int(d.format_error_turns)}`],
            ['Turn latency p50 / p95', `${seconds(d.median_turn_latency_s)} / ${seconds(d.p95_turn_latency_s)}`],
            ['Generation speed / slot p50', `${tokPerSec(d.generation_tokens_per_second)} / ${seconds(d.median_slot_wall_time_s)}`],
            ['First published', model.first_published ? date(model.first_published) : 'Unpublished'],
          ])}
          <h4>Run Provenance</h4>
          ${figures([
            ['Pricing SHA-256', (meta.pricing_sha256 || '—').slice(0, 16)],
            ['Probe receipt SHA-256', (meta.receipt_sha256 || '—').slice(0, 16)],
            ['Withheld bundle SHA-256', (meta.withheld_bundle_sha256 || '—').slice(0, 16)],
            ['System channel / sampling', `${meta.system_prompt_channel || '—'} / ${String(meta.sampling || '—')}`],
          ])}
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
  }
  render();
  return store.subscribe(render);
}
