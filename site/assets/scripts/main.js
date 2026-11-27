import { pct, int, date } from './format.js';
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

export function fillConfirmatory(root, report) {
  const summary = root.querySelector('#full-suite-summary');
  const table = root.querySelector('.full-suite-results');
  const safety = root.querySelector('#full-suite-safety');
  const rows = root.querySelector('#full-suite-rows');
  const entries = report?.entries || [];
  rows.replaceChildren();
  table.hidden = safety.hidden = entries.length === 0;
  if (!entries.length) {
    summary.textContent = 'No completed full-suite runs are published yet.';
    return;
  }
  summary.textContent =
    `${int(entries.length)} entries ranked by safety-adjusted score on ${int(report.scenarios)} fresh private scenarios. Exact scores share a rank.`;
  if (entries.every((entry) => typeof entry.probe_receipt_bound === 'boolean')) {
    const bound = entries.filter((entry) => entry.probe_receipt_bound).length;
    summary.textContent += ` ${int(bound)} of ${int(entries.length)} runs have a bound context-probe receipt.`;
  }
  for (const [index, entry] of entries.entries()) {
    const row = document.createElement('tr');
    const rank = document.createElement('td');
    const tied = entries[index - 1]?.rank === entry.rank || entries[index + 1]?.rank === entry.rank;
    rank.textContent = tied ? `T${int(entry.rank)}` : int(entry.rank);
    row.append(rank);
    const model = document.createElement('th');
    model.scope = 'row';
    model.textContent = `${entry.name} `;
    const config = document.createElement('span');
    config.className = 'full-suite-config';
    const effort = entry.reasoning_effort === 'transport_default' ? 'Default' : entry.reasoning_effort;
    config.textContent = `${effort[0].toUpperCase() + effort.slice(1)} · ${entry.transport} `;
    model.append(config);
    const receipt = document.createElement('span');
    receipt.className = 'full-suite-config';
    receipt.textContent = entry.probe_receipt_bound === undefined ? 'Probe receipt status unavailable'
      : entry.probe_receipt_bound ? 'Bound probe receipt' : 'No bound probe receipt';
    model.append(receipt);
    const terminal = [
      [entry.step_limit_slots, 'turn limit'],
      [entry.parse_failure_slots, 'parse failure'],
      [entry.refusal_slots, 'refusal'],
    ].filter(([count]) => count > 0)
      .map(([count, label]) => `${int(count)} ${label}${count === 1 ? '' : 's'}`);
    if (terminal.length) {
      const note = document.createElement('span');
      note.className = 'full-suite-config';
      note.textContent = terminal.join(', ');
      model.append(note);
    }
    row.append(model);
    const values = [
      pct(entry.safety_adjusted_score_tenths / (10 * report.scenarios), 2),
      `${int(entry.resolved_slots)}/${int(report.scenarios)}`,
    ];
    for (const value of values) {
      const cell = document.createElement('td');
      cell.textContent = value;
      row.append(cell);
    }
    const totals = document.createElement('td');
    totals.className = 'full-suite-totals';
    for (const [count, label] of [[entry.og_total, 'OG'], [entry.ab_total, 'AB']]) {
      const value = document.createElement('span');
      value.textContent = `${int(count)} ${label}`;
      if (totals.childNodes.length) totals.append(document.createTextNode(' '));
      totals.append(value);
    }
    row.append(totals);
    rows.append(row);
  }
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
  fillConfirmatory(document.getElementById('full-suite'), data.confirmatory);
  mountUpdates(document.getElementById('updates-root'), data.confirmatory);
  document.getElementById('load-status').hidden = true;
  reveal();

  if (location.hash === '#full-suite' || location.hash === '#leaderboard' || location.hash.startsWith('#m=')) {
    requestAnimationFrame(() => document.getElementById('leaderboard').scrollIntoView({ block: 'start' }));
  }
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
