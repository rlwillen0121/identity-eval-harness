import { END_TO_END_INTERVAL_METHOD, endToEndPct, int, date, safetyScreenLabels } from './format.js';
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
  document.getElementById('interval-method').textContent = END_TO_END_INTERVAL_METHOD;
  const checks = safetyScreenLabels(data.screen);
  document.getElementById('method-screen-count').textContent = int(checks.length);
  const checkList = document.getElementById('method-screen-checks');
  for (const label of checks) {
    const item = document.createElement('li');
    item.textContent = label;
    checkList.append(item);
  }
  const [lead, aside] = heroParts(data);
  if (lead) {
    const title = document.getElementById('hero-title');
    const quiet = document.createElement('span');
    quiet.className = 'hero-aside';
    quiet.textContent = aside;
    title.replaceChildren(`${lead} `, quiet);
  }
  document.getElementById('turn-budget').textContent = int(data.suite.turn_budget);
  document.getElementById('leaderboard-lede').textContent = leaderboardLede(data);
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

export function leaderboardLede(data) {
  return `Models are ranked by the ${int(data.suite.scenarios)} scenarios they resolve end to end: every wave correct, nothing unsafe, nothing missed. Tasks solved, out of ${int(data.suite.tasks)}, gives partial credit; simple fixed policies can pass most tasks but resolve no scenario.`;
}

// The headline in two parts: the leader's result, then how the rest of the field did.
export function heroParts(data) {
  const models = data.models;
  const top = [...models].sort((a, b) => a.rank - b.rank)[0];
  if (!top) return ['', ''];
  const rates = models.map((model) => model.g_srr).sort((a, b) => a - b);
  const middle = Math.floor(rates.length / 2);
  const median = rates.length % 2 ? rates[middle] : (rates[middle - 1] + rates[middle]) / 2;
  const summary = median === 0
    ? `${int(models.filter((model) => model.g_srr === 0).length)} of ${int(models.length)} models resolve none.`
    : `The median model resolves ${endToEndPct(median)}.`;
  return [`${top.name} resolves ${endToEndPct(top.g_srr)} of enterprise identity scenarios end to end.`, summary];
}

export function heroSentence(data) {
  return heroParts(data).filter(Boolean).join(' ');
}

// Chart labels are measured on a canvas, so wait briefly for the web fonts before drawing.
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
    ['chart-field', () => import('./charts/field.js')],
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

if (typeof document !== 'undefined') {
  boot().catch((error) => {
    console.error('Unable to load IdentityBench results', error);
    const status = document.getElementById('load-status');
    reveal();
    status.hidden = false;
    status.classList.add('is-error');
    status.textContent = 'Results could not be loaded. Please reload the page or use the full report linked in Method.';
  });
}
