// Only the DOM surface used by the charts; layout is explicit, never inferred from CSS.
class Node {
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
    this.classList = {
      add: (...names) => { this.className = [...new Set([...this.className.split(/\s+/).filter(Boolean), ...names])].join(' '); },
      contains: name => this.className.split(/\s+/).includes(name)
    };
  }
  get children() { return this.childNodes.filter(n => n.nodeType === 1); }
  get attributes() { return [...this.attrs].map(([name, value]) => ({name, value})); }
  get className() { return this.getAttribute('class') || ''; }
  set className(value) { this.setAttribute('class', value); }
  get textContent() { return this.nodeType === 3 ? this.nodeValue : this.childNodes.map(n => n.textContent).join(''); }
  set textContent(value) { this.replaceChildren(new Node('', 3, value == null ? '' : String(value))); }
  set innerHTML(value) { this._html = value; this.replaceChildren(); }
  get innerHTML() { return this._html || ''; }
  setAttribute(name, value) { this.attrs.set(name, String(value)); }
  getAttribute(name) { return this.attrs.get(name) ?? null; }
  removeAttribute(name) { this.attrs.delete(name); }
  append(...nodes) {
    for (let node of nodes) {
      if (typeof node === 'string') node = new Node('', 3, node);
      node.remove();
      node.parentNode = this;
      this.childNodes.push(node);
    }
  }
  replaceChildren(...nodes) {
    for (const child of this.childNodes) child.parentNode = null;
    this.childNodes = [];
    this.append(...nodes);
  }
  remove() {
    if (this.parentNode) this.parentNode.childNodes = this.parentNode.childNodes.filter(n => n !== this);
    this.parentNode = null;
  }
  contains(node) { return this === node || this.childNodes.some(n => n.contains(node)); }
  matches(selector) {
    const attr = selector.match(/\[([^=\]]+)(?:="([^"]*)")?\]/);
    const base = selector.split('[')[0];
    const baseMatch = !base || base === '*' || (base.startsWith('.') ? this.classList.contains(base.slice(1)) :
      base.startsWith('#') ? this.getAttribute('id') === base.slice(1) : this.tagName === base);
    return baseMatch && (!attr || (this.attrs.has(attr[1]) && (attr[2] === undefined || this.getAttribute(attr[1]) === attr[2])));
  }
  querySelectorAll(selector) {
    return this.children.flatMap(child => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  addEventListener(type, callback) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(callback);
  }
  dispatchEvent(event) {
    event.target ||= this;
    event.preventDefault ||= () => { event.defaultPrevented = true; };
    for (const callback of this.listeners.get(event.type) || []) callback(event);
    return !event.defaultPrevented;
  }
  focus() {
    if (document.activeElement === this) return;
    document.activeElement?.dispatchEvent({type: 'blur'});
    document.activeElement = this;
    this.dispatchEvent({type: 'focus'});
  }
  blur() { if (document.activeElement === this) document.activeElement = null; this.dispatchEvent({type: 'blur'}); }
  getBoundingClientRect() {
    const tip = this.classList.contains('chart-tooltip');
    const width = this.rect?.width ?? (tip ? 180 : Number(this.getAttribute('width')) || this.parentNode?.getBoundingClientRect().width || 360);
    const height = this.rect?.height ?? (tip ? 120 : Number(this.getAttribute('height')) || 1200);
    const left = this.rect?.left || 0;
    const top = this.rect?.top || 0;
    return {left, top, width, height, right: left + width, bottom: top + height};
  }
}

export function installDOM() {
  const observers = new Set();
  let canvasMeasureText = null;
  const margin = node => {
    if (node.classList.contains('chart-sub')) return {top: '8px', bottom: '16px'};
    if (node.classList.contains('chart-legend') || node.classList.contains('chart-caption')) return {top: '12px', bottom: '0px'};
    return {top: '0px', bottom: '0px'};
  };
  const body = new Node('body');
  globalThis.document = {
    body,
    activeElement: null,
    createElement: tag => new Node(tag),
    createElementNS: (_, tag) => new Node(tag),
    createTextNode: value => new Node('', 3, String(value))
  };
  globalThis.window = {innerWidth: 1120};
  globalThis.getComputedStyle = node => ({
    fontFamily: 'Test Sans',
    getPropertyValue: property => {
      const key = property === 'margin-top' ? 'top' : property === 'margin-bottom' ? 'bottom' : null;
      return key ? margin(node)[key] : property === '--sans' ? 'Test Sans' : '';
    },
    get marginTop() { return margin(node).top; },
    get marginBottom() { return margin(node).bottom; }
  });
  Node.prototype.getContext = function(type) {
    if (this.tagName !== 'canvas' || type !== '2d' || !canvasMeasureText) return null;
    const context = {font: ''};
    context.measureText = value => ({width: canvasMeasureText(String(value), context.font)});
    return context;
  };
  globalThis.ResizeObserver = class {
    constructor(callback) { this.callback = callback; observers.add(this); }
    observe(node) { this.node = node; }
    disconnect() { observers.delete(this); }
  };
  return {
    root(width = 360, height = 1200) {
      const root = new Node('div');
      root.rect = {width, height};
      return root;
    },
    resize(root, width = root.rect.width, height = root.rect.height) {
      root.rect.width = width;
      root.rect.height = height;
      for (const observer of observers) if (observer.node === root) observer.callback();
    },
    setViewportWidth(width) { globalThis.window.innerWidth = width; },
    setCanvasMeasureText(measure) { canvasMeasureText = measure; }
  };
}

export function makeStore(patch = {}) {
  let state = {sort: {key: 'rank', dir: 'asc'}, vendors: new Set(), query: '', selected: null, hover: null, ...patch};
  const subscribers = new Set();
  return {
    get: () => state,
    set(next) { state = {...state, ...next}; for (const callback of subscribers) callback(state); },
    subscribe(callback) { subscribers.add(callback); return () => subscribers.delete(callback); }
  };
}
