import { pct, int, date } from './format.js';
import { mount as mountTable } from './table.js';
import { mount as mountUpdates } from './updates.js';

function sameStoreValue(key, left, right) {
  if (key === 'vendors' && left instanceof Set && right instanceof Set) {
    return left.size === right.size && [...left].every((value) => right.has(value));
  }
  if (key === 'sort' && left && right && typeof left === 'object' && typeof right === 'object') {
    const keys = new Set([...Object.keys(left), ...Object.keys(right)]);
    return [...keys].every((name) => Object.is(left[name], right[name]));
  }
  return Object.is(left, right);
}

export function createStore(initial) {
  let state = initial;
  const listeners = new Set();
  return {
    get: () => state,
    set(patch) {
      const next = { ...state, ...patch };
      const selecting = Boolean(patch.selected);
      if (selecting) {
        next.query = '';
        next.vendors = new Set();
      }
      const repeatedSelection = selecting && patch.selected === state.selected;
      const unchanged = Object.keys(patch).every((key) => sameStoreValue(key, next[key], state[key]));
      if (unchanged && !repeatedSelection) return;
      state = next;
      [...listeners].forEach((fn) => fn(state, patch));
    },
    subscribe(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
  };
}

export function leaderboardLede(data) {
  if (!data.models?.length) {
    return `Awaiting the first completed ${int(data.suite.episodes)}-scenario ranked entry.`;
  }
  const top = data.models[0];
  return `${top.name} leads at ${pct(top.scores?.g_srr, 1)} strict end-to-end resolution across ${int(data.suite.episodes)} scenarios.`;
}

function fillSummary(data) {
  const models = data.models || [];
  document.getElementById('leaderboard-lede').textContent = leaderboardLede(data);
  const topScore = models.length ? pct(Math.max(...models.map((m) => m.scores?.g_srr ?? 0)), 1) : '—';
  const figures = [
    [int(models.length), 'ranked entries'],
    [int(data.suite.episodes), 'scenarios'],
    [int(data.suite.categories), 'IGA categories'],
    [topScore, 'top G-SRR'],
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
  const rows = [
    ['Suite', `${data.suite.id} (${data.suite.visibility})`],
    ['Protocol', data.protocol_id],
    ['Episodes SHA-256', data.suite.episodes_sha256],
    ['Oracles SHA-256', data.suite.oracles_sha256],
    ['Spec SHA-256', data.suite.spec_sha256],
    ['Commit C_K', data.suite.c_k || '—'],
    ['drand round', data.suite.round ? `${data.suite.round} (beacon ${data.suite.beacon_randomness_hex || 'pending'})` : '—'],
    ['Pricing SHA-256', data.pricing_sha256 || 'Set at leaderboard build'],
  ];
  for (const [label, value] of rows) {
    const dt = document.createElement('dt');
    const dd = document.createElement('dd');
    dt.textContent = label;
    dd.textContent = String(value);
    provenance.append(dt, dd);
  }
  const contam = document.getElementById('contamination-note');
  if (contam && data.contamination_notice) {
    contam.textContent = data.contamination_notice;
  }
  document.getElementById('updated-date').textContent = `Updated ${date(data.updated)}`;
  document.getElementById('published-date').textContent = `Results updated ${date(data.updated)}`;
}

function setupTheme() {
  const button = document.getElementById('theme-toggle');
  const os = window.matchMedia('(prefers-color-scheme: dark)');
  const isDark = () => (document.documentElement.dataset.theme
    ? document.documentElement.dataset.theme === 'dark' : os.matches);
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
    try { localStorage.setItem('identitybench-theme', theme); } catch { /* ignore */ }
    render();
  });
  os.addEventListener('change', render);
  render();
}

async function fontsReady(limit = 1500) {
  if (!document.fonts?.load) return;
  const fonts = Promise.all([document.fonts.load('400 12px Inter'), document.fonts.load('400 10px "Geist Mono"')]);
  await Promise.race([fonts.catch(() => {}), new Promise((resolve) => setTimeout(resolve, limit))]);
}

function reveal() {
  document.documentElement.classList.remove('is-loading');
  document.documentElement.classList.add('is-ready');
}

async function boot() {
  setupTheme();
  const response = await fetch(new URL('../../data/leaderboard.json', import.meta.url));
  if (!response.ok) throw new Error(`Data request failed (${response.status})`);
  const data = await response.json();
  const ids = new Set((data.models || []).map((m) => m.id));
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
    ['chart-pareto', () => import('./charts/pareto.js')],
    ['chart-safety', () => import('./charts/safety.js')],
    ['chart-marginals', () => import('./charts/marginals.js')],
    ['chart-categories', () => import('./charts/categories.js')],
  ];
  await fontsReady();
  await Promise.all(charts.map(async ([id, load]) => {
    const root = document.getElementById(id);
    try {
      const chartMod = await load();
      await chartMod.mount(root, data, store);
    } catch (error) {
      console.error(`Chart unavailable: ${id}`, error);
      const note = document.createElement('p');
      note.className = 'status-note';
      note.textContent = 'Chart unavailable';
      root.replaceChildren(note);
    }
  }));
}

if (typeof document !== 'undefined') {
  boot().catch((error) => {
    console.error('Unable to load IdentityBench v5.2 results', error);
    const status = document.getElementById('load-status');
    reveal();
    status.hidden = false;
    status.classList.add('is-error');
    status.textContent = 'Results could not be loaded. Run node site/v52/build.mjs or reload.';
  });
}
