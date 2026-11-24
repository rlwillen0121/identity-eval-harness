import {escapeHTML} from '../format.js';
import {vendorLogo, vendorLogoBody} from '../logos.js';

const NS = 'http://www.w3.org/2000/svg';

export function el(tag, attrs = {}, children = []) {
  const node = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  for (const child of children) node.append(typeof child === 'string' ? document.createTextNode(child) : child);
  return node;
}

export function linear([a, b], [c, d]) {
  return value => a === b ? (c + d) / 2 : c + (value - a) / (b - a) * (d - c);
}

export function log([a, b], range) {
  const scale = linear([Math.log(a), Math.log(b)], range);
  return value => scale(Math.log(value));
}

export function power(domain, range, exponent = 0.4) {
  const transform = value => Math.sign(value) * Math.abs(value) ** exponent;
  const scale = linear(domain.map(transform), range);
  return value => scale(transform(value));
}

export function ticks(min = 0, max = 1, count = 4) {
  if (count <= 0) return [min];
  return Array.from({length: count + 1}, (_, i) => min + (max - min) * i / count);
}

export function text(x, y, value, attrs = {}) {
  return el('text', {x, y, class: 'chart-text', ...attrs}, [String(value)]);
}

export function tipContent(model, lines) {
  return `<strong>${escapeHTML(model.name)}</strong><span class="chart-tip-vendor">${vendorLogo(model.vendor, model.vendor_label)}${escapeHTML(model.vendor_label)}</span>` +
    lines.map(([label, value]) => `<div><span>${escapeHTML(label)}</span><b>${escapeHTML(value)}</b></div>`).join('') +
    '<small class="chart-tip-hint">Click to open in the table</small>';
}

export function tipLabel(model, lines) {
  return `${model.name}, ${model.vendor_label}. ${lines.map(([a, b]) => `${a}: ${b}`).join('. ')}`;
}

export function tooltip(root) {
  const node = document.createElement('div');
  node.className = 'chart-tooltip';
  node.setAttribute('role', 'tooltip');
  node.hidden = true;
  root.append(node);
  let modelId = null;
  let changed = () => {};
  return {
    get modelId() { return modelId; },
    subscribe(fn) { changed = fn; },
    show(html, x, y, id) {
      node.innerHTML = html;
      node.hidden = false;
      const bounds = root.getBoundingClientRect();
      const size = node.getBoundingClientRect();
      const left = x + 12 + size.width <= bounds.width ? x + 12 : x - 12 - size.width;
      const top = y - 12 - size.height >= 0 ? y - 12 - size.height : y + 12;
      node.style.left = `${Math.max(0, Math.min(left, bounds.width - size.width))}px`;
      node.style.top = `${Math.max(0, Math.min(top, bounds.height - size.height))}px`;
      if (modelId !== id) { modelId = id; changed(); }
    },
    hide() {
      node.hidden = true;
      if (modelId !== null) { modelId = null; changed(); }
    }
  };
}

export function markPosition(svg, root, x, y) {
  const rect = svg.getBoundingClientRect();
  const outer = root.getBoundingClientRect();
  const [, , width, height] = svg.getAttribute('viewBox').split(' ').map(Number);
  return [rect.left - outer.left + x * rect.width / width,
    rect.top - outer.top + y * rect.height / height];
}

// Conservatively estimate text width so labels remain separated without a font or canvas dependency.
export function placeLabels(points, bounds, occupied = []) {
  const boxes = occupied.slice();
  const labels = [];
  for (const point of points) {
    const width = point.labelWidth ?? Array.from(point.label).length * 7.2 + 6;
    const height = 16;
    const centered = Math.max(bounds.left, Math.min(point.x - width / 2, bounds.right - width));
    const candidates = point.candidates || [
      {x: point.x + 9, y: point.y - 8}, {x: point.x - width - 9, y: point.y - 8},
      {x: centered, y: point.y - height - 9}, {x: centered, y: point.y + 9}
    ];
    for (const candidate of candidates) {
      const {x, y} = candidate;
      const box = {x, y, width, height};
      if (x < bounds.left || y < bounds.top || x + width > bounds.right || y + height > bounds.bottom) continue;
      if (boxes.some(b => x < b.x + b.width + 3 && x + width + 3 > b.x && y < b.y + b.height + 3 && y + height + 3 > b.y)) continue;
      labels.push({...point, box, leader: candidate.leader || null});
      boxes.push(box);
      break;
    }
  }
  return labels;
}

export function matches(model, state) {
  const query = (state.query || '').trim().toLocaleLowerCase('en-US');
  const vendor = !state.vendors?.size || state.vendors.has(model.vendor);
  return vendor && `${model.name} ${model.vendor_label} ${model.id}`.toLocaleLowerCase('en-US').includes(query);
}

export function markState(model, state, base = 'var(--ink-2)', opacity = 1) {
  const filtered = !matches(model, state);
  const hovered = model.id === state.hover;
  const selected = model.id === state.selected;
  const active = hovered || selected;
  const receded = Boolean(state.hover) && !hovered;
  return {active, hovered, selected, filtered,
    color: !filtered && active ? 'var(--accent)' : base,
    opacity: filtered || receded ? 0.3 : active ? 1 : opacity};
}

function setHover(store, id) {
  if (store.get().hover !== id) store.set({hover: id});
}

// These nodes survive store updates, preserving keyboard focus and pointer capture.
export function chart(root, title, subtitle, store, draw) {
  root.classList.add('analysis-chart');
  const header = document.createElement('div');
  header.className = 'chart-header';
  const heading = document.createElement('h3');
  heading.className = 'chart-title';
  heading.textContent = title;
  header.append(heading);
  const sub = document.createElement('p');
  sub.className = 'chart-sub';
  sub.textContent = subtitle;
  const canvas = document.createElement('div');
  canvas.className = 'chart-canvas';
  root.replaceChildren(...(title ? [header, sub] : []), canvas);
  const tip = tooltip(root);
  let sync = () => {};
  let width = 0;
  tip.subscribe(() => sync(store.get()));
  const redraw = () => {
    const focused = canvas.contains(document.activeElement) ? document.activeElement.getAttribute('data-focus-key') : null;
    width = Math.max(1, root.getBoundingClientRect().width || 360);
    tip.hide();
    canvas.replaceChildren();
    sync = draw({width, canvas, header, sub, tip, root}) || (() => {});
    sync(store.get());
    if (focused) {
      const node = Array.from(canvas.querySelectorAll('[data-focus-key]')).find(n => n.getAttribute('data-focus-key') === focused);
      node?.focus({preventScroll: true});
    }
  };
  const unsubscribe = store.subscribe(() => sync(store.get()));
  const observer = new ResizeObserver(() => {
    if (Math.abs(root.getBoundingClientRect().width - width) > 0.5) redraw();
  });
  redraw();
  observer.observe(root);
  return {header, redraw, destroy() { observer.disconnect(); unsubscribe?.(); tip.hide(); }};
}

export function svgCanvas(canvas, width, height, summary) {
  const svg = el('svg', {viewBox: `0 0 ${width} ${height}`, width, height, role: 'group', 'aria-label': summary, class: 'chart-svg'});
  canvas.append(svg);
  return svg;
}

export function yAxis(svg, plot, scale, values, format) {
  for (const value of values) {
    const y = scale(value);
    svg.append(el('line', {x1: plot.left, x2: plot.right, y1: y, y2: y, class: 'chart-grid'}),
      text(plot.left - 9, y + 3, format(value), {'text-anchor': 'end', class: 'chart-tick'}));
  }
  svg.append(el('line', {x1: plot.left, x2: plot.right, y1: plot.bottom, y2: plot.bottom, class: 'chart-axis'}));
}

export function stepPath(points, right) {
  if (!points.length) return '';
  return `M${points[0].x},${points[0].y}` + points.slice(1).map(p => `H${p.x}V${p.y}`).join('') + `H${right}`;
}

export function bindMark(node, model, lines, store, tip, position) {
  const show = () => {
    setHover(store, model.id);
    const [x, y] = position();
    tip.show(tipContent(model, lines), x, y, model.id);
  };
  node.setAttribute('tabindex', '0');
  node.setAttribute('role', 'button');
  node.setAttribute('aria-label', tipLabel(model, lines));
  node.setAttribute('data-model-id', model.id);
  node.setAttribute('data-focus-key', `${model.id}:${lines[0][0]}`);
  node.addEventListener('focus', show);
  node.addEventListener('blur', () => { setHover(store, null); tip.hide(); });
  node.addEventListener('click', () => store.set({selected: model.id}));
  node.addEventListener('keydown', event => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      store.set({selected: model.id});
    } else if (event.key === 'Escape') { setHover(store, null); tip.hide(); }
  });
  return {
    show,
    move: show,
    leave() {
      if (document.activeElement !== node) { setHover(store, null); tip.hide(); }
    }
  };
}

// All scatter charts share nearest-point interaction, focus, linked highlighting, and labels.
export const stepObstacles = (points, right, pad = 2) => {
  const boxes = [];
  for (let i = 0; i < points.length; i++) {
    const current = points[i];
    const next = points[i + 1];
    const end = next?.x ?? right;
    boxes.push({x: Math.min(current.x, end) - pad, y: current.y - pad,
      width: Math.abs(end - current.x) + pad * 2, height: pad * 2 + 1});
    if (next) boxes.push({x: next.x - pad, y: Math.min(current.y, next.y) - pad,
      width: pad * 2 + 1, height: Math.abs(current.y - next.y) + pad * 2});
  }
  return boxes;
};

const frontierCandidates = point => {
  const width = Array.from(point.label).length * 7.2 + 6;
  const centered = Math.max(point.plot.left, Math.min(point.x - width / 2, point.plot.right - width));
  return [
    {x: point.x - width - 9, y: point.y - 25},
    {x: point.x - width - 9, y: point.y - 8},
    {x: centered, y: point.y - 25},
    {x: point.x - width - 9, y: point.y + 1},
    {x: centered, y: point.y + 9}
  ];
};

const selectedCandidates = (point, plot) => {
  const width = Array.from(point.label).length * 7.2 + 6;
  const centered = Math.max(plot.left, Math.min(point.x - width / 2, plot.right - width));
  const result = [
    {x: point.x + 9, y: point.y - 8}, {x: point.x - width - 9, y: point.y - 8},
    {x: centered, y: point.y - 25}, {x: centered, y: point.y + 9}
  ];
  for (let dy = -24; dy >= -plot.bottom + plot.top; dy -= 24) result.push({x: centered, y: point.y + dy});
  for (let dy = 24; dy <= plot.bottom - plot.top; dy += 24) result.push({x: centered, y: point.y + dy});
  const grid = [];
  for (let y = plot.top; y <= plot.bottom - 16; y += 8) for (let x = plot.left; x <= plot.right - width; x += 8) {
    const nearestX = Math.max(x, Math.min(point.x, x + width));
    const nearestY = Math.max(y, Math.min(point.y, y + 16));
    const distance = Math.hypot(point.x - nearestX, point.y - nearestY);
    if (distance >= 10) grid.push({x, y, distance, nearestX, nearestY});
  }
  grid.sort((a, b) => a.distance - b.distance || a.y - b.y || a.x - b.x);
  result.push(...grid.map(candidate => ({x: candidate.x, y: candidate.y, leader: candidate.distance > 14 ? {
    x1: point.x, y1: point.y, x2: candidate.nearestX, y2: candidate.nearestY
  } : null})));
  return result;
};

const safetyCandidates = point => {
  const width = Array.from(point.label).length * 7.2 + 6;
  const offsets = [0, -10, 10, -20, 20, -30, 30, -40, 40, -50, 50, -60, 60];
  const result = [];
  for (const dy of offsets) {
    const x = Math.max(point.plot.left, Math.min(point.x - width - 10, point.plot.right - width));
    const y = Math.max(point.plot.top, Math.min(point.y + dy - 8, point.plot.bottom - 16));
    const distance = Math.hypot(point.x - Math.max(x, Math.min(point.x, x + width)), point.y - Math.max(y, Math.min(point.y, y + 16)));
    result.push({x, y, leader: distance > 14 ? {x1: point.x, y1: point.y, x2: Math.max(x, Math.min(point.x, x + width)),
      y2: Math.max(y, Math.min(point.y, y + 16))} : null});
  }
  for (const dx of [-60, -50, -40, -30, -20, -10, 10, 20, 30, 40, 50, 60]) {
    const x = Math.max(point.plot.left, Math.min(point.x + dx - width / 2, point.plot.right - width));
    const y = Math.max(point.plot.top, Math.min(point.y - 18, point.plot.bottom - 16));
    const distance = Math.hypot(point.x - Math.max(x, Math.min(point.x, x + width)), point.y - Math.max(y, Math.min(point.y, y + 16)));
    result.push({x, y, leader: distance > 14 ? {x1: point.x, y1: point.y, x2: Math.max(x, Math.min(point.x, x + width)),
      y2: Math.max(y, Math.min(point.y, y + 16))} : null});
  }
  return result;
};

const safetyFallbackCandidates = point => {
  const width = point.labelWidth ?? Array.from(point.label).length * 7.2 + 6;
  const candidates = [];
  for (let y = point.plot.top; y <= point.plot.bottom - 16; y += 8) {
    for (let x = point.plot.left; x <= point.plot.right - width; x += 8) {
      const nearestX = Math.max(x, Math.min(point.x, x + width));
      const nearestY = Math.max(y, Math.min(point.y, y + 16));
      const distance = Math.hypot(point.x - nearestX, point.y - nearestY);
      candidates.push({x, y, distance, leader: distance > 14 ? {
        x1: point.x, y1: point.y, x2: nearestX, y2: nearestY
      } : null});
    }
  }
  return candidates.sort((a, b) => a.distance - b.distance || a.y - b.y || a.x - b.x);
};

function safetyLeader(point, box) {
  const x = Math.max(box.x, Math.min(point.x, box.x + box.width));
  const y = Math.max(box.y, Math.min(point.y, box.y + box.height));
  if (Math.hypot(point.x - x, point.y - y) <= 14) return null;
  return {x1: point.x, y1: point.y, x2: x, y2: y};
}

export function scatter(svg, points, plot, store, tip, root, labelIds = new Set(), options = {}) {
  const marks = el('g');
  const leaders = el('g', {'pointer-events': 'none', class: 'chart-label-leaders'});
  const labels = el('g', {'pointer-events': 'none'});
  const nodes = new Map();
  for (const p of points) {
    const node = el('g', {class: 'chart-mark'});
    const radius = p.radius ?? (p.check ? 5 : 4.5);
    node.append(el('circle', {cx: p.x, cy: p.y, r: radius, stroke: p.ring || 'var(--surface)', 'stroke-width': p.strokeWidth ?? 2}));
    if (p.check) node.append(el('path', {d: `M${p.x - 2.8},${p.y}l1.8,1.8l3.7,-4`, fill: 'none', stroke: 'var(--surface)', 'stroke-width': 1.5}));
    const events = bindMark(node, p.model, p.lines, store, tip,
      () => markPosition(svg, root, p.x, p.y));
    marks.append(node);
    nodes.set(p, {node, events});
  }
  const [, , width, height] = svg.getAttribute('viewBox').split(' ').map(Number);
  const hitLeft = Math.max(0, plot.left - 10);
  const hitTop = Math.max(0, plot.top - 10);
  const overlay = el('rect', {x: hitLeft, y: hitTop, width: Math.min(width, plot.right + 10) - hitLeft,
    height: Math.min(height, plot.bottom + 10) - hitTop, fill: 'transparent', class: 'chart-hit-area', 'aria-hidden': 'true'});
  let nearest = null;
  const locate = event => {
    const rect = svg.getBoundingClientRect();
    const vb = svg.getAttribute('viewBox').split(' ').map(Number);
    const x = (event.clientX - rect.left) * vb[2] / rect.width;
    const y = (event.clientY - rect.top) * vb[3] / rect.height;
    let distance = 28 * 28;
    nearest = null;
    for (const p of points) {
      const d = (p.x - x) ** 2 + (p.y - y) ** 2;
      if (d < distance) { distance = d; nearest = p; }
    }
    return nearest;
  };
  overlay.addEventListener('pointermove', event => {
    locate(event);
    if (nearest) nodes.get(nearest).events.move(event);
    else { setHover(store, null); tip.hide(); }
  });
  overlay.addEventListener('pointerleave', () => {
    nearest = null;
    const focused = points.find(p => nodes.get(p).node === document.activeElement);
    if (focused) nodes.get(focused).events.show();
    else { setHover(store, null); tip.hide(); }
  });
  overlay.addEventListener('click', event => {
    locate(event);
    if (nearest) store.set({selected: nearest.model.id});
  });
  svg.append(marks, leaders, labels, overlay);
  const obstacles = [...(options.obstacles || []),
    ...points.map(p => ({x: p.x - 5, y: p.y - 5, width: 10, height: 10}))];
  const withLabel = (p, expanded = false) => {
    const point = {...p, label: p.model.name, plot};
    const candidates = options.labelMode === 'frontier' ? frontierCandidates(point) :
      options.labelMode === 'safety' ? safetyCandidates(point) : undefined;
    return {...point, candidates: expanded ? [...(candidates || []), ...selectedCandidates(point, plot)] : candidates};
  };
  // Reserve default labels once. Hover and filtering never move the remaining labels.
  const fixedCandidates = points.filter(p => labelIds.has(p.model.id)).sort((a, b) => b.y - a.y)
    .map(p => withLabel(p));
  const fixed = placeLabels(fixedCandidates, plot, obstacles);
  if (options.labelMode === 'safety' && fixed.length < fixedCandidates.length) {
    const placedIds = new Set(fixed.map(p => p.model.id));
    const boxes = [...obstacles, ...fixed.map(p => p.box)];
    for (const point of fixedCandidates.filter(p => !placedIds.has(p.model.id))) {
      const fallback = {...point, candidates: safetyFallbackCandidates(point)};
      const [placed] = placeLabels([fallback], plot, boxes);
      if (placed) {
        fixed.push(placed);
        placedIds.add(placed.model.id);
        boxes.push(placed.box);
      }
    }
  }
  const fixedIds = new Set(fixed.map(p => p.model.id));
  const labelNodes = new Map();
  const drawLabel = (p, state) => {
    let entry = labelNodes.get(p.model.id);
    if (!entry) {
      entry = {label: text(0, 0, p.label, {class: 'chart-direct-label', 'data-label-model': p.model.id})};
      labels.append(entry.label);
      labelNodes.set(p.model.id, entry);
    }
    entry.label.setAttribute('x', p.box.x + 3);
    entry.label.setAttribute('y', p.box.y + 12);
    entry.label.setAttribute('display', 'inline');
    entry.label.setAttribute('opacity', state.hover && p.model.id !== state.hover ? 0.4 : 1);
    const leader = options.labelMode === 'safety' ? safetyLeader(p, p.box) : p.leader;
    if (leader) {
      if (!entry.leader) {
        entry.leader = el('line', {class: 'chart-label-leader', 'data-label-model': p.model.id});
        leaders.append(entry.leader);
      }
      for (const [key, value] of Object.entries(leader)) entry.leader.setAttribute(key, value);
      entry.leader.setAttribute('display', 'inline');
      entry.leader.setAttribute('opacity', state.hover && p.model.id !== state.hover ? 0.4 : 1);
    } else if (entry.leader) {
      entry.leader.setAttribute('display', 'none');
    }
  };
  return state => {
    const candidates = [];
    for (const p of points) {
      const {node} = nodes.get(p);
      const style = markState(p.model, state, p.color || 'var(--ink-2)', p.opacity ?? 1);
      node.setAttribute('fill', style.color);
      node.setAttribute('opacity', style.opacity);
      node.setAttribute('aria-pressed', p.model.id === state.selected ? 'true' : 'false');
      if (!style.filtered && tip.modelId !== p.model.id && style.active && !fixedIds.has(p.model.id)) candidates.push(p);
    }
    for (const entry of labelNodes.values()) {
      entry.label.setAttribute('display', 'none');
      entry.leader?.setAttribute('display', 'none');
    }
    for (const p of fixed) {
      const prioritySafetyLabel = options.labelMode === 'safety' && p.model.screen_clear;
      if (matches(p.model, state) && (prioritySafetyLabel || tip.modelId !== p.model.id)) drawLabel(p, state);
    }
    candidates.sort((a, b) => Number(b.model.id === state.selected) - Number(a.model.id === state.selected));
    const extra = placeLabels(candidates.map(p => withLabel(p, true)), plot, [...obstacles, ...fixed.map(p => p.box)]);
    for (const p of extra) drawLabel(p, state);
  };
}

function nameMeasureContext() {
  const context = document.createElement('canvas').getContext?.('2d');
  if (!context) return null;
  const family = globalThis.getComputedStyle?.(document.body)?.fontFamily || 'sans-serif';
  context.font = `400 12px ${family}`;
  return context;
}

function measuredNameWidth(name, context = nameMeasureContext()) {
  const width = context?.measureText(String(name)).width;
  return Number.isFinite(width) ? width : Array.from(String(name)).length * 6.7;
}

// Row names sit after a vendor mark: a 6px margin, the mark, then a 6px gap.
const ROW_LOGO = 12;
const NAME_INSET = 6 + ROW_LOGO + 6;

export function nameColumn(width, models = []) {
  const context = nameMeasureContext();
  const longest = Math.max(0, ...models.map(model => measuredNameWidth(model.name, context)));
  return Math.min(longest + NAME_INSET + 6, width * 0.38);
}

function fitModelName(name, width) {
  const context = nameMeasureContext();
  if (measuredNameWidth(name, context) <= width) return name;
  const chars = Array.from(name);
  while (chars.length && measuredNameWidth(`${chars.join('')}…`, context) > width) chars.pop();
  return chars.length ? `${chars.join('')}…` : '';
}

// Both row charts use the same measured name column, outline and linked emphasis.
export function modelRow(svg, model, {left, width, top, pitch, height}) {
  const row = el('g', {'data-row-model': model.id, class: 'chart-model-row'});
  const label = fitModelName(model.name, Math.max(0, left - NAME_INSET - 6));
  const name = text(NAME_INSET, top + height / 2 + 4, label, {class: 'chart-row-label'});
  const logo = el('g', {class: 'chart-row-logo', 'aria-hidden': 'true',
    transform: `translate(6 ${top + height / 2 - ROW_LOGO / 2}) scale(${ROW_LOGO / 24})`});
  logo.innerHTML = vendorLogoBody(model.vendor, model.vendor_label);
  const outline = el('rect', {x: 1, y: top - (pitch - height) / 2, width: width - 2,
    height: pitch, fill: 'none', stroke: 'none', 'stroke-width': 1.5, 'pointer-events': 'none'});
  row.append(logo, name, outline);
  svg.append(row);
  return {row, name, outline,
    sync(state, marks) {
      const style = markState(model, state);
      const nameOpacity = style.filtered ? 0.3 : state.hover && !style.hovered ? 0.4 : 1;
      name.setAttribute('opacity', nameOpacity);
      logo.setAttribute('opacity', nameOpacity);
      name.style.fontWeight = style.active && !style.filtered ? '600' : '400';
      outline.setAttribute('stroke', style.active && !style.filtered ? 'var(--accent)' : 'none');
      for (const mark of marks) {
        mark.setAttribute('opacity', style.opacity);
        mark.setAttribute('aria-pressed', state.selected === model.id ? 'true' : 'false');
      }
      if (row.getAttribute('role') === 'button') row.setAttribute('aria-pressed', state.selected === model.id ? 'true' : 'false');
      // The outline stays above the cells without moving focusable row nodes.
      row.append(outline);
    }
  };
}
