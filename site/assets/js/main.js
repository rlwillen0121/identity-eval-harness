import { pct, int, date } from './format.js';
import { mount as mountTable } from './table.js';

export function createStore(initial) {
  let state = initial;
  const listeners = new Set();
  return {
    get: () => state,
    set(patch) {
      const next = { ...state, ...patch };
      if (next.selected && next.selected !== state.selected) {
        next.query = '';
        next.vendors = new Set();
      }
      const repeatedSelection = patch.selected && patch.selected === state.selected;
      if (!repeatedSelection && Object.keys(patch).every((key) => next[key] === state[key])) return;
      state = next;
      [...listeners].forEach((fn) => fn(state, patch));
    },
    subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); },
  };
}

function setupTheme() {
  const button = document.getElementById('theme-toggle');
  const os = window.matchMedia('(prefers-color-scheme: dark)');
  const isDark = () => document.documentElement.dataset.theme
    ? document.documentElement.dataset.theme === 'dark' : os.matches;
  function render() {
    const dark = isDark();
    const label = `Switch to ${dark ? 'light' : 'dark'} theme`;
    button.setAttribute('aria-label', label);
    button.title = label;
    button.innerHTML = dark
      ? '<svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="3.5" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M10 1v2M10 17v2M1 10h2M17 10h2M3.6 3.6L5 5M15 15l1.4 1.4M3.6 16.4L5 15M15 5l1.4-1.4" stroke="currentColor" stroke-width="1.3"/></svg>'
      : '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M16.7 12.4A7 7 0 017.6 3.3a7 7 0 109.1 9.1z" fill="none" stroke="currentColor" stroke-width="1.3"/></svg>';
  }
  button.addEventListener('click', () => {
    const theme = isDark() ? 'light' : 'dark';
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem('identitybench-theme', theme); } catch { /* Theme still works without storage. */ }
    render();
  });
  os.addEventListener('change', render);
  render();
}

function fillSummary(data) {
  const models = data.models;
  const top = [...models].sort((a, b) => a.rank - b.rank)[0];
  const rates = models.map((m) => m.g_srr).sort((a, b) => a - b);
  const middle = Math.floor(rates.length / 2);
  const median = rates.length % 2 ? rates[middle] : (rates[middle - 1] + rates[middle]) / 2;
  const noneCount = models.filter((model) => model.g_srr === 0).length;
  const summary = median === 0
    ? `${int(noneCount)} of ${int(models.length)} models resolve none.`
    : `The median model resolves ${pct(median)}.`;
  if (top) document.getElementById('hero-title').textContent = `${top.name} resolves ${pct(top.g_srr)} of enterprise identity scenarios end to end. ${summary}`;
  document.getElementById('turn-budget').textContent = int(data.suite.turn_budget);
  const figures = [
    [int(models.length), 'models evaluated'],
    [int(data.suite.scenarios), 'scenarios'],
    [int(data.suite.decisions), 'decisions verified per run'],
    [`${int(models.filter((m) => m.screen_clear).length)} of ${int(models.length)}`, 'models clearing the safety screen'],
  ];
  const root = document.getElementById('hero-figures');
  for (const [value, label] of figures) {
    const group = document.createElement('div');
    const dt = document.createElement('dt'), dd = document.createElement('dd');
    dt.textContent = label;
    dd.textContent = value;
    group.append(dt, dd);
    root.append(group);
  }
  const provenance = document.getElementById('provenance');
  for (const [label, value] of [
    ['Suite', data.suite.id], ['Seed', String(data.suite.seed)], ['Published', date(data.suite.published)],
    ['Episodes SHA-256', data.suite.episodes_sha256], ['Oracles SHA-256', data.suite.oracles_sha256],
  ]) {
    const dt = document.createElement('dt'), dd = document.createElement('dd');
    dt.textContent = label;
    dd.textContent = value;
    provenance.append(dt, dd);
  }
  document.getElementById('updated-date').textContent = `Updated ${date(data.updated)}`;
  document.getElementById('published-date').textContent = `Results updated ${date(data.updated)}`;
}

async function boot() {
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
  document.getElementById('load-status').hidden = true;
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
    ['chart-anatomy', () => import('./charts/anatomy.js')],
    ['chart-timeline', () => import('./charts/timeline.js')],
  ];
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

boot().catch((error) => {
  console.error('Unable to load IdentityBench results', error);
  const status = document.getElementById('load-status');
  status.hidden = false;
  status.textContent = 'Results could not be loaded. Please reload the page or use the full report linked in Method.';
});
