import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { installDOM } from './dom-stub.mjs';
import { boot } from '../assets/scripts/main.js';

const leaderboardJson = readFileSync(new URL('../data/leaderboard.json', import.meta.url), 'utf8');
const data = JSON.parse(leaderboardJson);

function createElementTree() {
  const byId = new Map();
  class BootNode {
    constructor(tag = 'div', id = null) {
      this.tagName = tag;
      this.nodeType = 1;
      this.childNodes = [];
      this.parentNode = null;
      this.attrs = new Map();
      this.style = {};
      this.listeners = new Map();
      this.hidden = false;
      this.value = '';
      this._html = '';
      this.classList = {
        add: (...names) => { this.className = [...new Set([...this.className.split(/\s+/).filter(Boolean), ...names])].join(' '); },
        remove: (...names) => { this.className = this.className.split(/\s+/).filter((c) => c && !names.includes(c)).join(' '); },
        contains: (name) => this.className.split(/\s+/).includes(name),
        toggle: (name, force) => {
          const has = this.classList.contains(name);
          const next = force === undefined ? !has : Boolean(force);
          if (next) this.classList.add(name);
          else this.classList.remove(name);
          return next;
        },
      };
      this.dataset = new Proxy({}, {
        get: (_, key) => this.attrs.get(`data-${String(key).replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`)}`),
        set: (_, key, val) => { this.setAttribute(`data-${String(key).replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`)}`, val); return true; },
      });
      if (id) {
        this.setAttribute('id', id);
        byId.set(id, this);
      }
    }
    get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
    get attributes() { return [...this.attrs].map(([name, value]) => ({ name, value })); }
    get className() { return this.getAttribute('class') || ''; }
    set className(value) { this.setAttribute('class', value); }
    get textContent() { return this._text ?? this.childNodes.map((n) => n.textContent).join(''); }
    set textContent(value) { this._text = value == null ? '' : String(value); this.childNodes = []; }
    set innerHTML(value) { this._html = String(value); this.childNodes = []; }
    get innerHTML() { return this._html; }
    setAttribute(name, value) { this.attrs.set(name, String(value)); }
    getAttribute(name) { return this.attrs.get(name) ?? null; }
    hasAttribute(name) { return this.attrs.has(name); }
    removeAttribute(name) { this.attrs.delete(name); }
    append(...nodes) {
      for (const node of nodes) {
        if (typeof node === 'string') continue;
        node.parentNode = this;
        this.childNodes.push(node);
      }
    }
    replaceChildren(...nodes) { this.childNodes = []; this._text = undefined; this.append(...nodes); }
    remove() {
      if (this.parentNode) this.parentNode.childNodes = this.parentNode.childNodes.filter((n) => n !== this);
      this.parentNode = null;
    }
    contains(node) { return this === node || this.childNodes.some((n) => n.contains(node)); }
    addEventListener(type, cb) {
      if (!this.listeners.has(type)) this.listeners.set(type, []);
      this.listeners.get(type).push(cb);
    }
    removeEventListener(type, cb) {
      if (!this.listeners.has(type)) return;
      this.listeners.set(type, this.listeners.get(type).filter((fn) => fn !== cb));
    }
    dispatchEvent(event) {
      event.target ||= this;
      event.preventDefault ||= () => { event.defaultPrevented = true; };
      for (const cb of this.listeners.get(event.type) || []) cb(event);
      return !event.defaultPrevented;
    }
    matches(selector) {
      const base = selector.split('[')[0];
      if (base.startsWith('.')) return this.classList.contains(base.slice(1));
      if (base.startsWith('#')) return this.getAttribute('id') === base.slice(1);
      return !base || base === '*' || this.tagName === base;
    }
    querySelectorAll(selector) {
      const parts = selector.split(',').map((s) => s.trim());
      return this.children.flatMap((child) => [
        ...(parts.some((p) => child.matches(p)) ? [child] : []),
        ...child.querySelectorAll(selector),
      ]);
    }
    querySelector(selector) {
      if (selector === '.leaderboard-table > tbody') return this._tbody || (this._tbody = new BootNode('tbody'));
      if (selector === '.model-search') return this._search || (this._search = new BootNode('input'));
      if (selector === '.model-count') return this._count || (this._count = new BootNode('span'));
      if (selector === '.vendor-controls') return this._vendors || (this._vendors = new BootNode('div'));
      if (selector === 'thead') return this._thead || (this._thead = new BootNode('thead'));
      if (selector === '.model-detail') return null;
      return this.querySelectorAll(selector)[0] || null;
    }
    getBoundingClientRect() { return { left: 0, top: 0, width: 1120, height: 600, right: 1120, bottom: 600 }; }
    focus() {}
    scrollIntoView() {}
  }

  for (const id of [
    'updated-date', 'theme-toggle', 'load-status', 'leaderboard-lede', 'hero-figures',
    'leaderboard-root', 'chart-frontier', 'chart-safety', 'chart-outcomes', 'chart-anatomy',
    'updates-root', 'interval-method', 'provenance', 'published-date',
  ]) {
    new BootNode('div', id);
  }
  return { BootNode, byId };
}

test('full DOM boot hides #load-status, mounts 4 SVG charts, populates provenance, and toggles theme', async () => {
  const dom = installDOM();
  const { BootNode, byId } = createElementTree();
  const prevFetch = globalThis.fetch;
  const prevLocation = globalThis.location;
  const prevHistory = globalThis.history;
  const prevRAF = globalThis.requestAnimationFrame;
  const prevElement = globalThis.Element;
  const prevLocalStorage = globalThis.localStorage;

  const storage = new Map();
  globalThis.Element = BootNode;
  globalThis.localStorage = { getItem: (k) => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, String(v)) };
  globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => JSON.parse(leaderboardJson) });
  globalThis.location = { hash: '', pathname: '/', search: '' };
  globalThis.history = { pushState: () => {} };
  globalThis.requestAnimationFrame = (cb) => cb();

  const origCreateElement = document.createElement;
  const origCreateElementNS = document.createElementNS;
  document.getElementById = (id) => byId.get(id) || null;
  document.documentElement = new BootNode('html');
  document.createElement = (tag) => {
    const node = origCreateElement(tag);
    node.rect = { width: 1120, height: 600 };
    return node;
  };
  document.createElementNS = origCreateElementNS;
  window.addEventListener = () => {};
  window.matchMedia = () => ({ matches: false, addEventListener() {} });

  try {
    await boot();
    assert.equal(byId.get('load-status').hidden, true);
    assert.equal(byId.get('hero-figures').children.length, 4);
    assert.ok(byId.get('provenance').children.length >= 10);
    assert.match(byId.get('updated-date').textContent, /^Updated /);
    assert.match(byId.get('leaderboard-root').innerHTML, /Show reference &amp; baselines/);
    assert.ok(byId.get('updates-root').innerHTML.includes(data.models[0].name));

    for (const chartId of ['chart-frontier', 'chart-safety', 'chart-outcomes', 'chart-anatomy']) {
      const svgs = byId.get(chartId).querySelectorAll('svg');
      assert.equal(svgs.length, 1, `${chartId} should mount 1 SVG`);
    }

    const themeBtn = byId.get('theme-toggle');
    themeBtn.dispatchEvent({ type: 'click' });
    assert.equal(document.documentElement.dataset.theme, 'dark');
    assert.equal(storage.get('identitybench-theme'), 'dark');
    themeBtn.dispatchEvent({ type: 'click' });
    assert.equal(document.documentElement.dataset.theme, 'light');
    assert.equal(storage.get('identitybench-theme'), 'light');
  } finally {
    globalThis.fetch = prevFetch;
    globalThis.location = prevLocation;
    globalThis.history = prevHistory;
    globalThis.requestAnimationFrame = prevRAF;
    globalThis.Element = prevElement;
    globalThis.localStorage = prevLocalStorage;
  }
  void dom;
});
