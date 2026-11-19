import { END_TO_END_INTERVAL_METHOD, endToEndPct, int, date } from './format.js';
import { createStore } from './store.js';
import { setupTheme, fontsReady, reveal } from './page.js';
import { mount as mountTable } from './table.js';
import { mount as mountUpdates } from './updates.js';

export { createStore };

function fillSummary(data) {
  const models = data.models;
  document.getElementById('interval-method').textContent = END_TO_END_INTERVAL_METHOD;
  document.getElementById('leaderboard-lede').textContent = leaderboardLede(data);
  const topModel = models.reduce((best, item) => ((item.g_srr || 0) > (best?.g_srr || -1) ? item : best), models[0]);
  const figures = [
    [int(models.length), 'models'],
    [int(data.suite.slots_per_run), `trials (${int(data.suite.episodes)} × ${int(data.suite.repeats)})`],
    [int(data.suite.decisions), 'decisions checked per run'],
    [endToEndPct(topModel?.g_srr ?? 0), 'top end-to-end'],
  ];
  const root = document.getElementById('hero-figures');
  root.replaceChildren();
  for (const [value, label] of figures) {
    const group = document.createElement('div');
    const dt = document.createElement('dt');
    const dd = document.createElement('dd');
    dt.textContent = label;
    dd.textContent = value;
    group.append(dt, dd);
    root.append(group);
  }
  const provenance = document.getElementById('provenance');
  provenance.replaceChildren();
  for (const [label, value] of [
    ['Suite', data.suite.id],
    ['Seed', String(data.suite.seed)],
    ['Normative contract', data.suite.normative_contract],
    ['Published', date(data.suite.published)],
    ['Seal SHA-256', data.suite.seal_sha256],
    ['Episodes SHA-256', data.suite.episodes_sha256],
    ['Oracles SHA-256', data.suite.oracles_sha256],
  ]) {
    const dt = document.createElement('dt');
    const dd = document.createElement('dd');
    dt.textContent = label;
    dd.textContent = value;
    provenance.append(dt, dd);
  }
  document.getElementById('updated-date').textContent = `Updated ${date(data.updated)}`;
  document.getElementById('published-date').textContent = `Results updated ${date(data.updated)}`;
}

export function leaderboardLede(data) {
  return `Ranked by the ${int(data.suite.slots_per_run)} trials each resolves end to end: both phases correct, nothing unsafe, nothing missed.`;
}

export async function boot() {
  setupTheme();
  const response = await fetch(new URL('../../data/leaderboard.json', import.meta.url));
  if (!response.ok) throw new Error(`Data request failed (${response.status})`);
  const data = await response.json();
  const ids = new Set(data.models.map((m) => m.id));
  function selectionFromHash() {
    if (!location.hash.startsWith('#m=')) return null;
    try {
      const id = decodeURIComponent(location.hash.slice(3));
      return ids.has(id) ? id : null;
    } catch { return null; }
  }
  const store = createStore({ sort: { key: 'rank', dir: 'asc' }, vendors: new Set(), query: '', selected: selectionFromHash(), hover: null });
  fillSummary(data);
  mountTable(document.getElementById('leaderboard-root'), data, store);
  mountUpdates(document.getElementById('updates-root'), data, store);
  document.getElementById('load-status').hidden = true;
  reveal();
  let lastSelected = store.get().selected;
  function scrollToModel(id) {
    requestAnimationFrame(() => document.getElementById(`model-${id}`)?.scrollIntoView({ block: 'start' }));
  }
  store.subscribe(({ selected }, patch) => {
    if (selected === lastSelected) {
      if (selected && patch.selected === selected) scrollToModel(selected);
      return;
    }
    lastSelected = selected;
    const hash = selected ? `#m=${encodeURIComponent(selected)}` : (location.hash.startsWith('#m=') ? '' : location.hash);
    if (hash !== location.hash) history.pushState(null, '', `${location.pathname}${location.search}${hash}`);
    if (selected) scrollToModel(selected);
  });
  window.addEventListener('hashchange', () => store.set({ selected: selectionFromHash() }));
  if (lastSelected) scrollToModel(lastSelected);

  const charts = [
    ['chart-frontier', () => import('./charts/frontier.js')],
    ['chart-safety', () => import('./charts/safety.js')],
    ['chart-outcomes', () => import('./charts/outcomes.js')],
    ['chart-anatomy', () => import('./charts/anatomy.js')],
  ];
  await fontsReady();
  await Promise.all(charts.map(async ([id, load]) => {
    const root = document.getElementById(id);
    try {
      const chart = await load();
      await chart.mount(root, data, store);
    } catch (error) {
      console.error(`Chart unavailable: ${id}`, error);
      const note = document.createElement('p');
      note.className = 'status-note';
      note.textContent = 'Chart unavailable';
      root.replaceChildren(note);
    }
  }));
}

if (typeof document !== 'undefined' && document.getElementById?.('leaderboard-root')) {
  boot().catch((error) => {
    console.error('Unable to load IdentityBench results', error);
    const status = document.getElementById('load-status');
    reveal();
    status.hidden = false;
    status.classList.add('is-error');
    status.textContent = 'Results could not be loaded. Please reload the page or use the methodology links below.';
  });
}
