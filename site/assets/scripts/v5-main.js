import { int, date } from './format.js';
import { createStore } from './main.js';
import { mount as mountTable, mountIncomplete } from './v5-table.js';

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
    try { localStorage.setItem('identitybench-theme', theme); } catch { /* Theme works without storage. */ }
    render();
  });
  os.addEventListener('change', render);
  render();
}

export function v5LeaderboardLede(data) {
  return `${int(data.audit.protocol_complete_count)} protocol-complete configurations (${int(data.suite.slots_per_run)} slots each) are ranked below; ${int(data.audit.provider_incomplete_count)} provider-incomplete runs are separated below the charts.`;
}

export function v5CalloutSummary(data) {
  return `${data.canonical_benchmark} remains the formal published benchmark. These ${int(data.audit.total_configurations)} ${data.suite.id} (${data.suite.spec_version}) configurations (${int(data.suite.episodes)} episodes × ${int(data.suite.repeats_per_episode)} repeats = ${int(data.suite.slots_per_run)} slots per run, ${int(data.audit.recorded_slots_total)} slots total) are historical Python-executed runs regraded byte-for-byte identically (${int(data.audit.byte_identical_summaries)}/${int(data.audit.total_configurations)} summaries & reports, ${int(data.audit.semantic_delta_count)} semantic deltas) by the native Rust identitybench v5 binary. Formal v5.0 sign-off remains open pending:`;
}

function fillSummary(data) {
  document.getElementById('v5-leaderboard-lede').textContent = v5LeaderboardLede(data);
  document.getElementById('v5-callout-summary').textContent = v5CalloutSummary(data);

  const heroRoot = document.getElementById('v5-hero-figures');
  for (const [value, label] of [
    [int(data.audit.protocol_complete_count), 'complete configurations'],
    [int(data.audit.provider_incomplete_count), 'provider-incomplete (separated)'],
    [int(data.audit.recorded_slots_total), 'audited slots (48 × 3 each)'],
    [int(data.suite.categories_count), 'IGA + agent categories'],
  ]) {
    const group = document.createElement('div');
    const dt = document.createElement('dt');
    const dd = document.createElement('dd');
    dt.textContent = label;
    dd.textContent = value;
    group.append(dt, dd);
    heroRoot.append(group);
  }

  const gateRoot = document.getElementById('v5-open-gates');
  for (const gate of data.audit.open_release_gates) {
    const li = document.createElement('li');
    const code = document.createElement('code');
    code.textContent = gate.id;
    li.append(code, document.createTextNode(` ${gate.label}`));
    gateRoot.append(li);
  }

  const t = data.audit.totals;
  const stripRoot = document.getElementById('v5-audit-strip');
  for (const [label, value] of [
    ['Completed slots', int(t.completed)],
    ['Provider errors', int(t.provider_errors)],
    ['Turn-limit stops', int(t.turn_limits)],
    ['Output-budget stops', int(t.output_limits)],
    ['Parse failures', int(t.parse_failures)],
    ['Context-limit stops', int(t.context_limits)],
  ]) {
    const wrap = document.createElement('div');
    const dt = document.createElement('dt');
    const dd = document.createElement('dd');
    dt.textContent = label;
    dd.textContent = value;
    wrap.append(dt, dd);
    stripRoot.append(wrap);
  }

  const provRoot = document.getElementById('v5-provenance');
  const auditDay = data.audit.finished_utc.slice(0, 10);
  for (const [label, value] of [
    ['Suite', `${data.suite.id} (${data.suite.topology}, ${data.suite.steering_mode}, ${data.suite.wave_execution_mode})`],
    ['Specification', `${data.suite.spec_version} (${data.suite.protocol_spec_sha256})`],
    ['Normative contract', data.suite.normative_contract_id],
    ['Seed', String(data.suite.seed)],
    ['SEAL.json SHA-256', data.suite.seal_sha256],
    ['Episodes SHA-256', data.suite.episodes_sha256],
    ['Oracles SHA-256', data.suite.oracles_sha256],
    ['Rust audit binary', `${data.audit.binary_name} (SHA-256 ${data.audit.binary_sha256})`],
    ['Execution origin', data.audit.execution_origin],
    ['Rust parity audit', `${int(data.audit.byte_identical_summaries)}/${int(data.audit.total_configurations)} byte-identical JSON & Markdown · ${int(data.audit.semantic_delta_count)} semantic deltas`],
  ]) {
    const dt = document.createElement('dt');
    const dd = document.createElement('dd');
    dt.textContent = label;
    dd.textContent = value;
    provRoot.append(dt, dd);
  }

  document.getElementById('v5-updated-date').textContent = `Audit ${date(auditDay)}`;
  document.getElementById('v5-published-date').textContent = `Offline Rust audit ${date(auditDay)}`;
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
  const response = await fetch(new URL('../../data/v5-preliminary.json', import.meta.url));
  if (!response.ok) throw new Error(`v5 data request failed (${response.status})`);
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
  mountTable(document.getElementById('v5-leaderboard-root'), data, store);
  mountIncomplete(document.getElementById('v5-incomplete-root'), data);
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
    ['v5-chart-resolution', () => import('./charts/v5-resolution.js')],
    ['v5-chart-cost', () => import('./charts/v5-cost.js')],
    ['v5-chart-outcomes', () => import('./charts/v5-outcomes.js')],
    ['v5-chart-categories', () => import('./charts/v5-categories.js')],
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
    console.error('Unable to load IdentityBench v5 preview results', error);
    const status = document.getElementById('load-status');
    reveal();
    status.hidden = false;
    status.classList.add('is-error');
    status.textContent = 'Preliminary v5 results could not be loaded. Please reload the page or view the audit report linked in Method.';
  });
}
