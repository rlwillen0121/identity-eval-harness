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

export function ticks(min = 0, max = 1, count = 4) {
  return Array.from({length: count + 1}, (_, i) => min + (max - min) * i / count);
}

export function text(x, y, value, attrs = {}) {
  return el('text', {x, y, class: 'chart-text', ...attrs}, [String(value)]);
}

export const escapeHTML = value => String(value).replace(/[&<>"']/g, char => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
})[char]);

export function tipContent(model, lines) {
  return `<strong>${escapeHTML(model.name)}</strong><span class="chart-tip-vendor">${escapeHTML(model.vendor_label)}</span>` +
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
  return {
    show(html, x, y) {
      node.innerHTML = html;
      node.hidden = false;
      const bounds = root.getBoundingClientRect();
      const size = node.getBoundingClientRect();
      const left = Math.max(0, Math.min(x + 12, bounds.width - size.width));
      const top = Math.max(0, Math.min(y + 12, bounds.height - size.height));
      node.style.left = `${left}px`;
      node.style.top = `${top}px`;
    },
    hide() { node.hidden = true; }
  };
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
  const active = hovered || selected && !state.hover;
  const receded = Boolean(state.hover) && !hovered;
  return {active, hovered, selected, filtered,
    color: filtered || receded ? 'var(--axis)' : hovered || selected ? 'var(--accent)' : base,
    opacity: filtered ? Math.min(opacity, 0.5) : receded ? Math.min(opacity, 0.55) : active ? 1 : opacity};
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
  root.replaceChildren(header, sub, canvas);
  const tip = tooltip(root);
  let sync = () => {};
  let width = 0;
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

export function bindMark(node, model, lines, store, tip, root, position) {
  const show = () => {
    setHover(store, model.id);
    const [x, y] = position();
    tip.show(tipContent(model, lines), x, y);
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
    move(event) {
      setHover(store, model.id);
      const rect = root.getBoundingClientRect();
      tip.show(tipContent(model, lines), event.clientX - rect.left, event.clientY - rect.top);
    },
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

export function scatter(svg, points, plot, store, tip, root, labelIds = new Set(), options = {}) {
  const marks = el('g');
  const leaders = el('g', {'pointer-events': 'none', class: 'chart-label-leaders'});
  const labels = el('g', {'pointer-events': 'none'});
  const nodes = new Map();
  for (const p of points) {
    const node = el('g', {class: 'chart-mark'});
    const radius = p.radius ?? (p.check ? 5 : 4.5);
    node.append(el('circle', {cx: p.x, cy: p.y, r: radius, 'stroke-width': 2, stroke: 'var(--surface)'}));
    if (p.check) node.append(el('path', {d: `M${p.x - 2.8},${p.y}l1.8,1.8l3.7,-4`, fill: 'none', stroke: 'var(--surface)', 'stroke-width': 1.5}));
    const events = bindMark(node, p.model, p.lines, store, tip, root, () => {
      const r = svg.getBoundingClientRect();
      const outer = root.getBoundingClientRect();
      const vb = svg.getAttribute('viewBox').split(' ').map(Number);
      return [r.left - outer.left + p.x * r.width / vb[2], r.top - outer.top + p.y * r.height / vb[3]];
    });
    marks.append(node);
    nodes.set(p, {node, events});
  }
  const overlay = el('rect', {x: plot.left - 10, y: plot.top - 10, width: plot.right - plot.left + 20,
    height: plot.bottom - plot.top + 20, fill: 'transparent', class: 'chart-hit-area', 'aria-hidden': 'true'});
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
  return state => {
    const active = [];
    const candidates = [];
    for (const p of points) {
      const {node} = nodes.get(p);
      const style = markState(p.model, state, p.color || 'var(--ink-2)', p.opacity ?? 1);
      node.setAttribute('fill', style.color);
      node.setAttribute('opacity', style.opacity);
      node.setAttribute('aria-pressed', p.model.id === state.selected ? 'true' : 'false');
      if (style.active) active.push(p);
      if (labelIds.has(p.model.id) || !style.filtered && style.active || p.model.id === state.selected) candidates.push(p);
    }
    active.sort((a, b) => Number(a.model.id === state.hover) - Number(b.model.id === state.hover));
    for (const p of active) marks.append(nodes.get(p).node);
    candidates.sort((a, b) => Number(labelIds.has(b.model.id)) - Number(labelIds.has(a.model.id)) ||
      Number(b.model.id === state.selected) - Number(a.model.id === state.selected) ||
      Number(b.model.id === state.hover) - Number(a.model.id === state.hover) || b.y - a.y);
    const candidatesWithLabels = candidates.map(p => ({...p, label: p.model.name,
      plot, candidates: options.labelMode === 'frontier' ? [...frontierCandidates({...p, label: p.model.name, plot}),
        ...(p.model.id === state.selected ? selectedCandidates({...p, label: p.model.name}, plot) : [])] :
        options.labelMode === 'safety' ? safetyCandidates({...p, label: p.model.name, plot}) :
          p.model.id === state.selected ? selectedCandidates({...p, label: p.model.name}, plot) : undefined}));
    const placed = placeLabels(candidatesWithLabels, plot, options.obstacles || []);
    leaders.replaceChildren(...placed.filter(p => p.leader).map(p => el('line', {...p.leader, class: 'chart-label-leader'})));
    labels.replaceChildren(...placed.map(p => text(p.box.x + 3, p.box.y + 12, p.label,
      {class: 'chart-direct-label', opacity: state.hover && p.model.id !== state.hover && p.model.id !== state.selected ? 0.65 : 1})));
  };
}
