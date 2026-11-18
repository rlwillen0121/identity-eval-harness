import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { hasVendorLogo } from '../../../site/assets/scripts/logos.js';
import { escapeHTML, int, pct } from '../../../site/assets/scripts/format.js';
import { v5ColumnDefinitions, v5FailureSummaryMarkup, v5ModelCellsMarkup, v5SortComparator, mount, mountIncomplete } from '../../../site/assets/scripts/v5-table.js';
import { v5CalloutSummary, v5LeaderboardLede } from '../../../site/assets/scripts/v5-main.js';
import * as v5Resolution from '../../../site/assets/scripts/charts/v5-resolution.js';
import * as v5Cost from '../../../site/assets/scripts/charts/v5-cost.js';
import * as v5Outcomes from '../../../site/assets/scripts/charts/v5-outcomes.js';
import * as v5Categories from '../../../site/assets/scripts/charts/v5-categories.js';
import { installDOM, makeStore } from './dom-stub.mjs';

const v5Data = JSON.parse(readFileSync(new URL('../../../site/data/v5-preliminary.json', import.meta.url), 'utf8'));

test('every vendor across all 24 v5 configurations has a mark in logos.js', () => {
  const allRuns = [...v5Data.models, ...v5Data.incomplete_runs];
  assert.equal(allRuns.length, 24);
  for (const run of allRuns) {
    assert.ok(hasVendorLogo(run.vendor), `missing vendor mark for ${run.vendor} (${run.id})`);
  }
});

test('v5.html and index.html link cleanly and preserve CSP and self-hosted fonts', () => {
  const site = new URL('../../../site/', import.meta.url);
  const v5Html = readFileSync(new URL('v5.html', site), 'utf8');
  const indexHtml = readFileSync(new URL('index.html', site), 'utf8');

  assert.match(indexHtml, /href="v5\.html"/);
  assert.match(indexHtml, /class="preview-banner"/);
  assert.match(indexHtml, /v4\.1 · full/);
  assert.match(v5Html, /href="\.\/"/);
  assert.match(v5Html, /<link rel="canonical" href="https:\/\/identitybench\.ai\/v5">/);
  assert.match(v5Html, /<meta property="og:url" content="https:\/\/identitybench\.ai\/v5">/);

  assert.doesNotMatch(v5Html, /<style[\s>]/);
  assert.doesNotMatch(v5Html, /<script(?![^>]*\ssrc="assets\/)[^>]*>/);
  assert.doesNotMatch(v5Html, /<link[^>]+href="https?:\/\/(?!identitybench\.ai\/)/);

  const tokens = readFileSync(new URL('assets/styles/tokens.css', site), 'utf8');
  const fontUrls = [...tokens.matchAll(/url\("\.\.\/fonts\/([^"]+)"\)/g)].map((m) => m[1]);
  const preloads = [...v5Html.matchAll(/<link rel="preload" href="([^"]+)" as="font" type="font\/woff2" crossorigin>/g)].map((m) => m[1]);
  assert.ok(preloads.length >= 1);
  for (const href of preloads) {
    assert.ok(existsSync(new URL(href, site)), href);
    assert.ok(fontUrls.includes(href.split('/').pop()));
  }

  assert.match(v5Html, /Preliminary Preview/i);
  assert.match(v5Html, /Provider-Incomplete Runs \(Not Comparable \/ Excluded from Ranking\)/);
  assert.match(v5Html, /<p class="study-note">[^<]*Independent exploratory research study/i);
  assert.match(v5Html, /<footer class="site-footer">[\s\S]*independent, exploratory research study[\s\S]*<\/footer>/i);

  assert.match(v5LeaderboardLede(v5Data), /18 protocol-complete/);
  assert.match(v5CalloutSummary(v5Data), /24\/24 summaries & reports/);
});

test('v5 failure markup keeps provider errors, parse failures, and turn limits separate', () => {
  const mixed = v5FailureSummaryMarkup({
    provider_errors: 0,
    parse_failures: 23,
    turn_limits: 121,
    output_limits: 0,
    context_limits: 0,
  });
  assert.match(mixed, /23 parse/);
  assert.match(mixed, /121 turn/);
  assert.doesNotMatch(mixed, /prov/);

  const prov = v5FailureSummaryMarkup({
    provider_errors: 140,
    parse_failures: 1,
    turn_limits: 1,
    output_limits: 0,
    context_limits: 2,
  });
  assert.match(prov, /140 prov/);
  assert.match(prov, /1 parse/);
  assert.match(prov, /1 turn/);
  assert.match(prov, /2 ctx/);
  assert.equal(v5FailureSummaryMarkup({}), '<span class="failure-zero">0</span>');
});

test('v5 primary table sorts cleanly, renders 7 categories, and separates incomplete runs', () => {
  const cols = v5ColumnDefinitions(v5Data);
  assert.equal(cols.length, 8);
  assert.deepEqual(cols.map(([k]) => k), ['rank', 'name', 'g_srr', 'sem_srr_commit', 'categories', 'safety', 'failures', 'cost_usd']);

  const bySem = [...v5Data.models].sort(v5SortComparator('sem_srr_commit', 'desc'));
  assert.equal(bySem[0].name, 'Claude Sonnet 5.5');

  const sample = v5Data.models[0];
  const rowHtml = v5ModelCellsMarkup(sample, v5Data);
  assert.equal((rowHtml.match(/class="category-track"/g) || []).length, 7);
  assert.ok(rowHtml.includes('Agent governance'));

  const stubEl = () => ({
    innerHTML: '', value: '', textContent: '',
    addEventListener() {}, replaceChildren() { this.innerHTML = ''; },
    querySelectorAll: () => [], querySelector: () => stubEl(),
  });
  const detail = stubEl();
  const body = {
    innerHTML: '',
    addEventListener() {},
    querySelectorAll: () => [],
    querySelector: (selector) => (selector === '.model-detail' && body.innerHTML.includes('class="model-detail"') ? detail : null),
  };
  const elements = {
    '.leaderboard-table > tbody': body,
    '.model-search': stubEl(),
    '.model-count': stubEl(),
    '.vendor-controls': stubEl(),
    thead: stubEl(),
  };
  const root = { innerHTML: '', querySelector: (sel) => elements[sel] || null, querySelectorAll: () => [] };

  const previousDocument = globalThis.document;
  globalThis.document = { activeElement: null };
  try {
    const store = makeStore({ selected: sample.id });
    const unmount = mount(root, v5Data, store);
    assert.match(body.innerHTML, /<tr class="detail-row"><td colspan="8"><div class="model-detail"/);
    assert.ok(detail.innerHTML.includes('Agent governance'));
    assert.ok(detail.innerHTML.includes('OVERGRANT@commit'));

    store.set({ selected: null, query: '__missing_model__' });
    assert.match(body.innerHTML, /<tr class="empty-row"><td colspan="8">/);
    unmount();
  } finally {
    globalThis.document = previousDocument;
  }

  const incRoot = { innerHTML: '' };
  mountIncomplete(incRoot, v5Data);
  for (const inc of v5Data.incomplete_runs) {
    assert.ok(incRoot.innerHTML.includes(escapeHTML(inc.run_id)), `missing ${inc.run_id}`);
    assert.ok(incRoot.innerHTML.includes(int(inc.failures.provider_errors)));
    assert.ok(incRoot.innerHTML.includes(pct(inc.provisional_bounds[1])));
  }
  assert.match(incRoot.innerHTML, /UNMEASURED/);
  assert.doesNotMatch(incRoot.innerHTML, /<th scope="col">#<\/th>/);
});

const v5Charts = { v5Resolution, v5Cost, v5Outcomes, v5Categories };
const chartStates = {
  rest: {},
  hover: { hover: v5Data.models[0].id },
  selected: { selected: v5Data.models[0].id },
  filtered: { vendors: new Set([v5Data.models[0].vendor]) },
};
const validAttributes = (root) => {
  for (const node of root.querySelectorAll('*')) {
    for (const { name, value } of node.attributes) {
      assert.doesNotMatch(value, /NaN|undefined|Infinity/, `${node.tagName}.${name}=${value}`);
    }
  }
};

for (const [name, chart] of Object.entries(v5Charts)) {
  for (const width of [328, 560, 1120]) {
    for (const [stateName, state] of Object.entries(chartStates)) {
      test(`${name} mounts at ${width}px in ${stateName} state`, () => {
        const dom = installDOM();
        const root = dom.root(width);
        const store = makeStore(state);
        const destroy = chart.mount(root, v5Data, store);
        validAttributes(root);
        const svgs = root.querySelectorAll('svg');
        assert.equal(svgs.length, 1);
        assert.equal(svgs[0].getAttribute('role'), 'group');
        assert.ok(svgs[0].getAttribute('aria-label')?.trim());
        const marks = root.querySelectorAll('[data-model-id]');
        assert.equal(new Set(marks.map((n) => n.getAttribute('data-model-id'))).size, v5Data.models.length);
        dom.resize(root, 328);
        validAttributes(root);
        destroy();
      });
    }
  }
}

function installV5HtmlDOM(htmlText) {
  const VOID_TAGS = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr']);
  const decodeEntities = (s) => s
    .replaceAll('&amp;', '&')
    .replaceAll('&lt;', '<')
    .replaceAll('&gt;', '>')
    .replaceAll('&quot;', '"')
    .replaceAll('&#39;', "'");
  const toCamel = (k) => k.replace(/-([a-z])/g, (_, ch) => ch.toUpperCase());
  const toKebab = (k) => k.replace(/[A-Z]/g, (ch) => `-${ch.toLowerCase()}`);

  class HtmlNode {
    constructor(tag = '', nodeType = 1, value = '') {
      this.tagName = tag;
      this.nodeType = nodeType;
      this.nodeValue = value;
      this.childNodes = [];
      this.parentNode = null;
      this.attrs = new Map();
      this.style = {};
      this.listeners = new Map();
      this.hidden = false;
      this._value = undefined;
      this.classList = {
        add: (...names) => {
          this.className = [...new Set([...this.className.split(/\s+/).filter(Boolean), ...names])].join(' ');
        },
        remove: (...names) => {
          const drop = new Set(names);
          this.className = this.className.split(/\s+/).filter((c) => c && !drop.has(c)).join(' ');
        },
        toggle: (name, force) => {
          const has = this.classList.contains(name);
          const next = force === undefined ? !has : Boolean(force);
          if (next) this.classList.add(name);
          else this.classList.remove(name);
          return next;
        },
        contains: (name) => this.className.split(/\s+/).filter(Boolean).includes(name),
      };
      this.dataset = new Proxy({}, {
        get: (_, prop) => this.attrs.get(`data-${toKebab(String(prop))}`),
        set: (_, prop, val) => {
          this.attrs.set(`data-${toKebab(String(prop))}`, String(val));
          return true;
        },
        deleteProperty: (_, prop) => this.attrs.delete(`data-${toKebab(String(prop))}`),
      });
    }
    get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
    get attributes() { return [...this.attrs].map(([name, value]) => ({ name, value })); }
    get className() { return this.getAttribute('class') || ''; }
    set className(value) { this.setAttribute('class', value); }
    get value() { return this._value ?? (this.getAttribute('value') || ''); }
    set value(v) { this._value = String(v); }
    get textContent() { return this.nodeType === 3 ? this.nodeValue : this.childNodes.map((n) => n.textContent).join(''); }
    set textContent(value) { this.replaceChildren(new HtmlNode('', 3, value == null ? '' : String(value))); }
    get innerHTML() { return this._html || ''; }
    set innerHTML(html) {
      this._html = String(html);
      const parsed = parseFragment(this._html);
      this.replaceChildren(...parsed);
    }
    setAttribute(name, value) {
      if (name === 'hidden') this.hidden = true;
      this.attrs.set(name, String(value));
    }
    getAttribute(name) { return this.attrs.get(name) ?? null; }
    hasAttribute(name) { return this.attrs.has(name); }
    removeAttribute(name) {
      if (name === 'hidden') this.hidden = false;
      this.attrs.delete(name);
    }
    append(...nodes) {
      for (let node of nodes) {
        if (typeof node === 'string') node = new HtmlNode('', 3, node);
        node.remove();
        node.parentNode = this;
        this.childNodes.push(node);
      }
    }
    after(...nodes) {
      if (!this.parentNode) return;
      const idx = this.parentNode.childNodes.indexOf(this);
      const prepared = nodes.map((n) => (typeof n === 'string' ? new HtmlNode('', 3, n) : n));
      for (const n of prepared) { n.remove(); n.parentNode = this.parentNode; }
      this.parentNode.childNodes.splice(idx + 1, 0, ...prepared);
    }
    replaceChildren(...nodes) {
      for (const child of this.childNodes) child.parentNode = null;
      this.childNodes = [];
      this.append(...nodes);
    }
    remove() {
      if (this.parentNode) this.parentNode.childNodes = this.parentNode.childNodes.filter((n) => n !== this);
      this.parentNode = null;
    }
    contains(node) { return this === node || this.childNodes.some((n) => n.contains(node)); }
    matches(selectorList) {
      return selectorList.split(',').some((sel) => {
        const tokens = sel.trim().replace(/\s*>\s*/g, ' > ').split(/\s+/);
        if (!this.matchesSimple(tokens[tokens.length - 1])) return false;
        let idx = tokens.length - 2;
        let cur = this.parentNode;
        while (idx >= 0) {
          if (tokens[idx] === '>') {
            idx -= 1;
            if (!cur || !cur.matchesSimple(tokens[idx])) return false;
            cur = cur.parentNode;
            idx -= 1;
          } else {
            while (cur && !cur.matchesSimple(tokens[idx])) cur = cur.parentNode;
            if (!cur) return false;
            cur = cur.parentNode;
            idx -= 1;
          }
        }
        return true;
      });
    }
    matchesSimple(selector) {
      let rest = selector.trim();
      if (!rest || rest === '*') return true;
      const attrs = [];
      rest = rest.replace(/\[([^=\]]+)(?:="([^"]*)")?\]/g, (_, k, v) => {
        attrs.push([k, v]);
        return '';
      });
      const classes = [];
      rest = rest.replace(/\.([a-zA-Z0-9_-]+)/g, (_, cls) => {
        classes.push(cls);
        return '';
      });
      let id = null;
      rest = rest.replace(/#([a-zA-Z0-9_-]+)/g, (_, ident) => {
        id = ident;
        return '';
      });
      const tag = rest || null;
      if (tag && this.tagName !== tag) return false;
      if (id && this.getAttribute('id') !== id) return false;
      if (!classes.every((c) => this.classList.contains(c))) return false;
      return attrs.every(([k, v]) => this.attrs.has(k) && (v === undefined || this.getAttribute(k) === v));
    }
    querySelectorAll(selector) {
      return this.children.flatMap((child) => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]);
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    closest(selector) {
      let cur = this;
      while (cur && cur.nodeType === 1) {
        if (cur.matches(selector)) return cur;
        cur = cur.parentNode;
      }
      return null;
    }
    addEventListener(type, callback) {
      if (!this.listeners.has(type)) this.listeners.set(type, []);
      this.listeners.get(type).push(callback);
    }
    dispatchEvent(event) {
      event.target ||= this;
      event.preventDefault ||= () => { event.defaultPrevented = true; };
      event.stopPropagation ||= () => { event.cancelBubble = true; };
      let cur = this;
      while (cur && !event.cancelBubble) {
        event.currentTarget = cur;
        for (const callback of cur.listeners.get(event.type) || []) callback(event);
        cur = cur.parentNode;
      }
      return !event.defaultPrevented;
    }
    focus() { globalThis.document.activeElement = this; }
    scrollIntoView() {}
    getBoundingClientRect() {
      const tip = this.classList.contains('chart-tooltip');
      const width = this.rect?.width ?? (tip ? 180 : Number(this.getAttribute('width')) || this.parentNode?.getBoundingClientRect().width || 1120);
      const height = this.rect?.height ?? (tip ? 120 : Number(this.getAttribute('height')) || 1200);
      return { left: 0, top: 0, width, height, right: width, bottom: height };
    }
  }

  function parseFragment(html) {
    const root = new HtmlNode('fragment');
    const stack = [root];
    const tokenRe = /<!--[\s\S]*?-->|<\/([a-zA-Z0-9-]+)\s*>|<([a-zA-Z0-9-]+)((?:\s+[^\s=/>]+(?:="[^"]*"|='[^']*')?)*)\s*(\/?)>|([^<]+)/g;
    let match;
    while ((match = tokenRe.exec(html))) {
      if (match[0].startsWith('<!--')) continue;
      if (match[1]) {
        const closing = match[1].toLowerCase();
        while (stack.length > 1 && stack[stack.length - 1].tagName !== closing) stack.pop();
        if (stack.length > 1) stack.pop();
      } else if (match[2]) {
        const tag = match[2].toLowerCase();
        const node = new HtmlNode(tag);
        const attrText = match[3] || '';
        const attrRe = /([^\s=/>]+)(?:=(?:"([^"]*)"|'([^']*)'))?/g;
        let attrMatch;
        while ((attrMatch = attrRe.exec(attrText))) {
          const val = attrMatch[2] ?? attrMatch[3] ?? '';
          node.setAttribute(attrMatch[1], decodeEntities(val));
        }
        stack[stack.length - 1].append(node);
        const selfClose = Boolean(match[4]) || VOID_TAGS.has(tag);
        if (!selfClose) stack.push(node);
      } else if (match[5]) {
        stack[stack.length - 1].append(new HtmlNode('', 3, decodeEntities(match[5])));
      }
    }
    return root.childNodes;
  }

  const docEl = new HtmlNode('html');
  docEl.classList.add('is-loading');
  const rootNodes = parseFragment(htmlText);
  const parsedHtml = rootNodes.find((n) => n.tagName === 'html');
  const bodyNode = parsedHtml?.querySelector('body') || new HtmlNode('body');
  docEl.append(bodyNode);

  const observers = new Set();
  const findById = (node, id) => {
    if (node.getAttribute('id') === id) return node;
    for (const child of node.children) {
      const hit = findById(child, id);
      if (hit) return hit;
    }
    return null;
  };
  const storage = new Map();
  const windowListeners = new Map();
  const loc = { href: 'https://identitybench.ai/v5', pathname: '/v5', search: '', hash: '' };
  globalThis.document = {
    documentElement: docEl,
    body: bodyNode,
    activeElement: null,
    getElementById: (id) => findById(docEl, id),
    createElement: (tag) => new HtmlNode(tag),
    createElementNS: (_, tag) => new HtmlNode(tag),
    createTextNode: (value) => new HtmlNode('', 3, String(value)),
    fonts: { load: async () => {} },
  };
  globalThis.location = loc;
  globalThis.history = {
    pushState(_state, _title, url) {
      const hashIdx = String(url).indexOf('#');
      loc.hash = hashIdx >= 0 ? String(url).slice(hashIdx) : '';
      loc.href = `https://identitybench.ai${url}`;
    },
  };
  globalThis.window = {
    innerWidth: 1120,
    location: loc,
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
    addEventListener(type, fn) {
      if (!windowListeners.has(type)) windowListeners.set(type, []);
      windowListeners.get(type).push(fn);
    },
  };
  const prevDesc = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    writable: true,
    value: {
      getItem: (k) => storage.get(k) ?? null,
      setItem: (k, v) => storage.set(k, String(v)),
    },
  });
  globalThis.requestAnimationFrame = (cb) => cb();
  globalThis.ResizeObserver = class {
    constructor(callback) { this.callback = callback; observers.add(this); }
    observe(node) { this.node = node; }
    disconnect() { observers.delete(this); }
  };
  globalThis.getComputedStyle = () => ({
    fontFamily: 'Test Sans',
    getPropertyValue: () => '0px',
    marginTop: '0px',
    marginBottom: '0px',
  });
  return { docEl, bodyNode, storage, restoreStorage: () => {
    if (prevDesc) Object.defineProperty(globalThis, 'localStorage', prevDesc);
    else delete globalThis.localStorage;
  } };
}

test('v5-main boots cleanly on v5.html DOM without main.js cross-firing, duplicate theme listeners, or error banners', async () => {
  const v5Html = readFileSync(new URL('../../../site/v5.html', import.meta.url), 'utf8');
  const prev = {
    document: globalThis.document,
    window: globalThis.window,
    location: globalThis.location,
    history: globalThis.history,
    requestAnimationFrame: globalThis.requestAnimationFrame,
    ResizeObserver: globalThis.ResizeObserver,
    getComputedStyle: globalThis.getComputedStyle,
    fetch: globalThis.fetch,
  };
  const fetchedUrls = [];
  let restoreStorage;
  try {
    const dom = installV5HtmlDOM(v5Html);
    const { docEl } = dom;
    restoreStorage = dom.restoreStorage;
    globalThis.fetch = async (url) => {
      const href = String(url);
      fetchedUrls.push(href);
      assert.match(href, /\/site\/data\/v5-preliminary\.json$/);
      return { ok: true, status: 200, json: async () => v5Data };
    };

    // Importing main.js on v5.html's DOM must NOT auto-boot, fetch leaderboard.json, or attach a duplicate #theme-toggle listener.
    await import(`../../../site/assets/scripts/main.js?v5-isolation=${Date.now()}`);
    assert.equal(fetchedUrls.length, 0);

    await import(`../../../site/assets/scripts/v5-main.js?v5-dom-boot=${Date.now()}`);
    for (let i = 0; i < 10; i++) await new Promise((resolve) => setTimeout(resolve, 5));

    assert.equal(fetchedUrls.length, 1);
    const status = globalThis.document.getElementById('load-status');
    assert.equal(status.hidden, true);
    assert.equal(status.classList.contains('is-error'), false);
    assert.equal(docEl.classList.contains('is-ready'), true);
    assert.equal(docEl.classList.contains('is-loading'), false);

    // Theme toggle must flip cleanly on each single click (no double listener).
    const toggle = globalThis.document.getElementById('theme-toggle');
    assert.equal(docEl.dataset.theme, undefined);
    toggle.dispatchEvent({ type: 'click' });
    assert.equal(docEl.dataset.theme, 'dark');
    toggle.dispatchEvent({ type: 'click' });
    assert.equal(docEl.dataset.theme, 'light');
    toggle.dispatchEvent({ type: 'click' });
    assert.equal(docEl.dataset.theme, 'dark');

    // Summary, 18 complete rows, 6 incomplete rows, and all 4 charts must render cleanly.
    assert.equal(globalThis.document.getElementById('v5-hero-figures').children.length, 4);
    assert.equal(globalThis.document.getElementById('v5-open-gates').children.length, 4);
    assert.equal(globalThis.document.getElementById('v5-audit-strip').children.length, 6);
    assert.equal(globalThis.document.getElementById('v5-provenance').children.length, 20);

    const completeRoot = globalThis.document.getElementById('v5-leaderboard-root');
    const incompleteRoot = globalThis.document.getElementById('v5-incomplete-root');
    assert.equal(completeRoot.querySelectorAll('.model-row').length, 18);
    assert.equal(incompleteRoot.querySelectorAll('tbody tr').length, 6);

    for (const chartId of ['v5-chart-resolution', 'v5-chart-cost', 'v5-chart-outcomes', 'v5-chart-categories']) {
      const chartRoot = globalThis.document.getElementById(chartId);
      assert.equal(chartRoot.querySelectorAll('svg').length, 1, `${chartId} rendered SVG`);
      assert.equal(chartRoot.querySelector('.status-note'), null, `${chartId} had no error fallback`);
    }

    // Verify interactive row selection, vendor filtering, and sorting work on the booted DOM.
    const firstRowBtn = completeRoot.querySelector('.model-row .model-name');
    firstRowBtn.dispatchEvent({ type: 'click' });
    assert.ok(completeRoot.querySelector('.model-detail'), 'clicking row opens v5-detail panel');
    assert.equal(globalThis.location.hash, `#m=${encodeURIComponent(v5Data.models[0].id)}`);

    const semSortBtn = completeRoot.querySelector('[data-sort="sem_srr_commit"]');
    semSortBtn.dispatchEvent({ type: 'click' });
    assert.equal(completeRoot.querySelector('.model-row').dataset.modelId, 'claude-sonnet-5-5-t0-high');

    const googleVendorBtn = completeRoot.querySelector('[data-vendor="google"]');
    googleVendorBtn.dispatchEvent({ type: 'click' });
    const googleCount = v5Data.models.filter((m) => m.vendor === 'google').length;
    assert.equal(completeRoot.querySelectorAll('.model-row').length, googleCount);

    const searchInput = completeRoot.querySelector('.model-search');
    searchInput.value = 'Hydrogen';
    searchInput.dispatchEvent({ type: 'input' });
    assert.equal(completeRoot.querySelectorAll('.model-row').length, 1);
  } finally {
    restoreStorage?.();
    globalThis.document = prev.document;
    globalThis.window = prev.window;
    globalThis.location = prev.location;
    globalThis.history = prev.history;
    globalThis.requestAnimationFrame = prev.requestAnimationFrame;
    globalThis.ResizeObserver = prev.ResizeObserver;
    globalThis.getComputedStyle = prev.getComputedStyle;
    globalThis.fetch = prev.fetch;
  }
});
