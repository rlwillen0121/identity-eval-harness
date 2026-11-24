// Minimal DOM stub for testing v5.2 views and SVG charts in Node (`node --test`).
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
    this.value = '';
    this.classList = {
      add: (...names) => {
        this.className = [...new Set([...this.className.split(/\s+/).filter(Boolean), ...names])].join(' ');
      },
      toggle: (name, force) => {
        const set = new Set(this.className.split(/\s+/).filter(Boolean));
        const has = set.has(name);
        const want = force === undefined ? !has : Boolean(force);
        if (want) set.add(name);
        else set.delete(name);
        this.className = [...set].join(' ');
        return want;
      },
      contains: (name) => this.className.split(/\s+/).includes(name),
    };
  }
  get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
  get dataset() {
    const target = this;
    return new Proxy({}, {
      get(_, prop) {
        const attr = `data-${String(prop).replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`)}`;
        return target.attrs.get(attr);
      },
    });
  }
  get attributes() { return [...this.attrs].map(([name, value]) => ({ name, value })); }
  get className() { return this.getAttribute('class') || ''; }
  set className(value) { this.setAttribute('class', value); }
  get textContent() { return this.nodeType === 3 ? this.nodeValue : this.childNodes.map((n) => n.textContent).join(''); }
  set textContent(value) { this.replaceChildren(new Node('', 3, value == null ? '' : String(value))); }
  set innerHTML(value) {
    this._html = String(value ?? '');
    this.replaceChildren();
    const voidTags = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr', 'path', 'line', 'circle', 'rect']);
    const stack = [this];
    const tokenRe = /<(\/?)([a-zA-Z0-9-]+)([^>]*)>|([^<]+)/g;
    let match;
    while ((match = tokenRe.exec(this._html))) {
      const [, closing, tag, attrStr, text] = match;
      const top = stack[stack.length - 1];
      if (text) {
        top.append(new Node('', 3, text));
        continue;
      }
      const tagName = tag.toLowerCase();
      if (closing) {
        for (let i = stack.length - 1; i >= 1; i -= 1) {
          if (stack[i].tagName === tagName) {
            stack.length = i;
            break;
          }
        }
        continue;
      }
      const el = new Node(tagName);
      const attrRe = /([a-zA-Z0-9:-]+)(?:="([^"]*)")?/g;
      let am;
      while ((am = attrRe.exec(attrStr))) {
        if (am[1] !== '/') el.setAttribute(am[1], am[2] ?? '');
      }
      top.append(el);
      const selfClosing = attrStr.trim().endsWith('/') || voidTags.has(tagName);
      if (!selfClosing) stack.push(el);
    }
  }
  get innerHTML() { return this._html || ''; }
  setAttribute(name, value) { this.attrs.set(name, String(value)); }
  getAttribute(name) { return this.attrs.get(name) ?? null; }
  hasAttribute(name) { return this.attrs.has(name); }
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
    if (this.parentNode) this.parentNode.childNodes = this.parentNode.childNodes.filter((n) => n !== this);
    this.parentNode = null;
  }
  contains(node) { return this === node || this.childNodes.some((n) => n.contains(node)); }
  matches(selector) {
    const attr = selector.match(/\[([^=\]]+)(?:="([^"]*)")?\]/);
    const base = selector.split('[')[0].trim();
    const baseMatch = !base || base === '*' || (base.startsWith('.')
      ? this.classList.contains(base.slice(1))
      : base.startsWith('#')
        ? this.getAttribute('id') === base.slice(1)
        : this.tagName === base);
    return baseMatch && (!attr || (this.attrs.has(attr[1]) && (attr[2] === undefined || this.getAttribute(attr[1]) === attr[2])));
  }
  querySelectorAll(selector) {
    const parts = selector.split(',').map((s) => s.trim()).filter(Boolean);
    if (parts.length > 1) {
      const seen = new Set();
      return parts.flatMap((part) => this.querySelectorAll(part)).filter((node) => {
        if (seen.has(node)) return false;
        seen.add(node);
        return true;
      });
    }
    const direct = selector.split('>').map((s) => s.trim());
    if (direct.length === 2) {
      return this.querySelectorAll(direct[0]).flatMap((parent) => parent.children.filter((c) => c.matches(direct[1])));
    }
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
  removeEventListener(type, callback) {
    if (!this.listeners.has(type)) return;
    this.listeners.set(type, this.listeners.get(type).filter((fn) => fn !== callback));
  }
  dispatchEvent(event) {
    event.target ||= this;
    event.preventDefault ||= () => { event.defaultPrevented = true; };
    for (const callback of this.listeners.get(event.type) || []) callback(event);
    return !event.defaultPrevented;
  }
  focus() {
    if (document.activeElement === this) return;
    document.activeElement?.dispatchEvent({ type: 'blur' });
    document.activeElement = this;
    this.dispatchEvent({ type: 'focus' });
  }
  blur() { if (document.activeElement === this) document.activeElement = null; this.dispatchEvent({ type: 'blur' }); }
  getBoundingClientRect() {
    const tip = this.classList.contains('chart-tooltip');
    const width = this.rect?.width ?? (tip ? 180 : Number(this.getAttribute('width')) || this.parentNode?.getBoundingClientRect().width || 720);
    const height = this.rect?.height ?? (tip ? 120 : Number(this.getAttribute('height')) || 400);
    return { left: 0, top: 0, width, height, right: width, bottom: height };
  }
}

export function installDOM() {
  const body = new Node('body');
  globalThis.Element = Node;
  globalThis.document = {
    body,
    activeElement: null,
    createElement: (tag) => new Node(tag),
    createElementNS: (_, tag) => new Node(tag),
    createTextNode: (value) => new Node('', 3, String(value)),
  };
  globalThis.window = { innerWidth: 1120, location: { href: 'https://identitybench.ai/v52/' } };
  globalThis.getComputedStyle = () => ({ fontFamily: 'Inter, sans-serif' });
  globalThis.ResizeObserver = class {
    observe() {}
    disconnect() {}
  };
  return { root: () => new Node('div'), body };
}
