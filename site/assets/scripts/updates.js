import { date, pct, escapeHTML as esc, int } from './format.js';
import { computeTieMeta } from './table.js';
import { vendorLogo } from './logos.js';

export function mount(root, data, store) {
  const byId = new Map((data.models || []).map((m) => [m.id, m]));
  const { labels } = computeTieMeta(data.models || []);
  const groups = (data.history || [])
    .map((h) => ({
      date: h.date || '',
      models: (h.models || []).map((id) => byId.get(id)).filter(Boolean).sort((a, b) => a.rank - b.rank),
    }))
    .filter((g) => g.models.length > 0)
    .sort((a, b) => b.date.localeCompare(a.date));
  if (groups.length) groups.at(-1).launch = true;

  const full = data.confirmatory;
  if (full?.published_on && full.entries?.length) {
    const fullUpdate = { scenarios: full.scenarios, count: full.entries.length };
    const sameDay = groups.find((g) => g.date === full.published_on);
    if (sameDay) sameDay.full = fullUpdate;
    else {
      groups.push({ date: full.published_on, full: fullUpdate, models: [] });
      groups.sort((a, b) => b.date.localeCompare(a.date));
    }
  }

  if (!groups.length) {
    root.innerHTML = '<p class="section-lede">No dated publication entries yet.</p>';
    return () => {};
  }

  root.innerHTML = `
    <p class="section-lede">Publication dates for the core and full leaderboards.</p>
    <div class="updates-ledger">
      ${groups.map((g) => `
        <section class="updates-day" aria-label="Published ${esc(date(g.date))}">
          <div class="updates-date-block">
            <time class="updates-date" datetime="${esc(g.date)}">${esc(date(g.date))}</time>
            ${g.launch ? '<span class="updates-launch">Launch</span>' : ''}
          </div>
          <ul class="updates-models">
            ${g.full ? `<li><a class="update-model-button update-full-link" href="#full-suite"><span class="update-model-name">${int(g.full.scenarios)}-scenario full suite</span><span class="update-model-meta">${int(g.full.count)} ranked</span></a></li>` : ''}
            ${g.models.map((m) => `<li><button class="update-model-button" type="button" data-model-id="${esc(m.id)}"><span class="update-model-name">${vendorLogo(m.vendor, m.vendor_label)}${esc(m.name)}</span><span class="update-model-meta">Core ${esc(labels.get(m.id))} · ${pct(m.scores?.safety_adjusted_score ?? m.scores?.g_srr, 1)}</span></button></li>`).join('')}
          </ul>
        </section>
      `).join('')}
    </div>
  `;

  function onClick(event) {
    const btn = event.target.closest('button[data-model-id]');
    if (btn) store.set({ selected: btn.dataset.modelId });
  }
  root.addEventListener('click', onClick);
  return () => root.removeEventListener('click', onClick);
}
