import { date, gSrrPct, escapeHTML as esc, int } from './format.js';
import { vendorLogo } from './logos.js';

export function mount(root, data, store) {
  const history = Array.isArray(data.history) ? data.history : [];
  if (!history.length || !(data.models || []).length) {
    root.innerHTML = '<p class="section-lede">Publication updates will appear here as v5.3 ranked runs are published.</p>';
    return () => {};
  }
  const byId = new Map(data.models.map((m) => [m.id, m]));
  const groups = history
    .map((h) => ({
      date: h.date || '',
      models: (h.models || []).map((id) => byId.get(id)).filter(Boolean).sort((a, b) => a.rank - b.rank),
    }))
    .filter((g) => g.models.length > 0)
    .sort((a, b) => b.date.localeCompare(a.date));

  if (!groups.length) {
    root.innerHTML = '<p class="section-lede">No dated publication entries yet.</p>';
    return () => {};
  }

  root.innerHTML = `
    <p class="section-lede">When each ${int(data.suite.episodes)}-scenario ranked entry was first published.</p>
    <div class="updates-ledger">
      ${groups.map((g, idx) => `<section class="updates-day" aria-label="Published ${esc(date(g.date))}"><div class="updates-date-block"><time class="updates-date" datetime="${esc(g.date)}">${esc(date(g.date))}</time>${idx === groups.length - 1 ? '<span class="updates-launch">Launch</span>' : ''}</div><ul class="updates-models">${g.models.map((m) => `<li><button class="update-model-button" type="button" data-model-id="${esc(m.id)}"><span class="update-model-name">${vendorLogo(m.vendor, m.vendor_label)}${esc(m.name)}</span><span class="update-model-meta">#${int(m.rank)} · ${gSrrPct(m.scores?.g_srr)}</span></button></li>`).join('')}</ul></section>`).join('')}
    </div>
  `;

  function onClick(event) {
    const btn = event.target.closest('.update-model-button');
    if (btn) store.set({ selected: btn.dataset.modelId });
  }
  root.addEventListener('click', onClick);
  return () => root.removeEventListener('click', onClick);
}
