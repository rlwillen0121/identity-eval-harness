import { int, date } from './format.js';
import { mount as mountFullTable } from './full-table.js';
import { mountAnalysis } from './charts/full.js';

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
      const repeatedSelection = patch.selected && patch.selected === state.selected;
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

function fillProvenance({ suite, updated }) {
  const provenance = document.getElementById('provenance');
  const rows = [
    ['Suite', `${suite.id} (private)`],
    ['Protocol', suite.protocol_id],
    ['Seal SHA-256', suite.seal_sha256],
    ['Episodes SHA-256', suite.episodes_sha256],
    ['Oracles SHA-256', suite.oracles_sha256],
    ['Spec SHA-256', suite.spec_sha256],
    ['Commit C_K', suite.c_k],
    ['drand round', suite.round],
  ];
  for (const [label, value] of rows) {
    const dt = document.createElement('dt');
    const dd = document.createElement('dd');
    dt.textContent = label;
    dd.textContent = String(value);
    provenance.append(dt, dd);
  }
  if (updated) {
    document.getElementById('updated-date').textContent = `Updated ${date(updated)}`;
    document.getElementById('published-date').textContent = `Results updated ${date(updated)}`;
  }
}

export function fillConfirmatory(root, report, selected = null) {
  const summary = root.querySelector('#full-suite-summary');
  const table = root.querySelector('.full-suite-results');
  const entries = report?.entries || [];
  table.replaceChildren();
  table.hidden = entries.length === 0;
  if (!entries.length) {
    summary.textContent = 'No completed full-suite runs are published yet.';
    return null;
  }
  summary.textContent = `${int(report.scenarios)} private enterprise access scenarios. A clean solve earns 1 point. Each safety error costs 0.1 points, up to 3 per scenario.`;
  const store = createStore({ sort: { key: 'rank', dir: 'asc' }, query: '', vendors: new Set(), selected });
  mountFullTable(table, report, store);
  return store;
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

function reveal() {
  document.documentElement.classList.remove('is-loading');
  document.documentElement.classList.add('is-ready');
}

async function boot() {
  setupTheme();
  const response = await fetch(new URL('../../data/leaderboard.json', import.meta.url), { cache: 'no-cache' });
  if (!response.ok) throw new Error(`Data request failed (${response.status})`);
  const data = await response.json();
  fillProvenance(data);
  const entries = data.confirmatory?.entries || [];
  if (entries.length && entries.every((entry) => typeof entry.probe_receipt_bound === 'boolean')) {
    const bound = entries.filter((entry) => entry.probe_receipt_bound).length;
    document.getElementById('receipt-coverage').textContent =
      `${int(bound)} of ${int(entries.length)} ranked runs recorded a fingerprint-bound context-probe receipt. The probe checks that the connection can carry the benchmark’s full request and return the expected response.`;
  }
  function fromHash() {
    if (!location.hash.startsWith('#m=')) return null;
    try {
      const id = decodeURIComponent(location.hash.slice(3));
      return entries.find((entry) => entry.id === id)?.id ?? null;
    } catch { return null; }
  }
  const store = fillConfirmatory(document.getElementById('full-suite'), data.confirmatory, fromHash());
  document.getElementById('load-status').hidden = true;
  reveal();

  function scrollToSelection() {
    const id = store?.get().selected;
    if (id || ['#full-suite', '#leaderboard'].includes(location.hash) || location.hash.startsWith('#m=')) {
      requestAnimationFrame(() => (document.getElementById(`model-${id}`) || document.getElementById('leaderboard')).scrollIntoView({ block: 'start' }));
    }
  }
  store?.subscribe(({ selected }, patch) => {
    if (!('selected' in patch)) return;
    const hash = selected ? `#m=${encodeURIComponent(selected)}` : '#leaderboard';
    if (hash !== location.hash && (selected || location.hash.startsWith('#m='))) {
      history.pushState(null, '', `${location.pathname}${location.search}${hash}`);
    }
  });
  window.addEventListener('hashchange', () => {
    const selected = fromHash();
    store?.set(selected ? { selected, query: '', vendors: new Set() } : { selected });
    scrollToSelection();
  });
  scrollToSelection();
  await document.fonts?.ready;
  mountAnalysis(document, data, store ?? createStore({ vendors: new Set() }), (id) => {
    const button = document.getElementById(`model-${id}`)?.querySelector('.model-name');
    button?.focus({ preventScroll: true });
    scrollToSelection();
  });
}

if (typeof document !== 'undefined') {
  boot().catch((error) => {
    console.error('Unable to load IdentityBench v5.3 results', error);
    const status = document.getElementById('load-status');
    reveal();
    status.hidden = false;
    status.classList.add('is-error');
    status.textContent = 'Results could not be loaded. Run node site/build.mjs or reload.';
  });
}
