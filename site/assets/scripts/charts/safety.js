import {int, pct} from '../format.js';
import {chart, el, linear, power, scatter, svgCanvas, text, ticks, yAxis} from './svg.js';

const minimumHeight = 354;

function outcomesPeer(root) {
  let parent = root.parentNode;
  while (parent) {
    const peer = parent.querySelector?.('#chart-outcomes');
    if (peer) return peer;
    parent = parent.parentNode;
  }
  return root.ownerDocument?.getElementById?.('chart-outcomes') || document.getElementById?.('chart-outcomes') || null;
}

function chromeHeight(root) {
  return ['.chart-header', '.chart-sub', '.chart-caption', '.chart-legend'].reduce((height, selector) => {
    const node = root.querySelector(selector);
    if (!node) return height;
    const rect = node.getBoundingClientRect();
    const style = globalThis.getComputedStyle?.(node);
    const marginTop = Number.parseFloat(style?.marginTop) || 0;
    const marginBottom = Number.parseFloat(style?.marginBottom) || 0;
    return height + rect.height + marginTop + marginBottom;
  }, 0);
}

function plotHeight(root, peer) {
  if (!peer || (globalThis.window?.innerWidth ?? 0) < 900) return minimumHeight;
  return Math.max(minimumHeight, peer.getBoundingClientRect().height - chromeHeight(root));
}

export function mount(root, data, store) {
  const subtitle = 'Unsafe committed actions against tasks solved.';
  const legend = document.createElement('p');
  legend.className = 'chart-legend';
  legend.textContent = '✓ Clears all seven safety checks · Shaded: tasks ≥ 80%, unsafe ≤ 1%';
  const peer = outcomesPeer(root);
  const view = chart(root, 'Capable and safe are different things', subtitle, store, ({width, canvas, tip}) => {
    canvas.append(legend);
    const height = plotHeight(root, peer);
    const plot = {left: 48, right: width - 18, top: 26, bottom: height - 54};
    const max = Math.max(0.1, Math.ceil(Math.max(...data.models.map(m => m.unsafe_rate)) * 10) / 10);
    const x = linear([0, 1], [plot.left + 6, plot.right - 6]);
    const y = power([0, max], [plot.bottom, plot.top], 0.4);
    const clear = data.models.filter(m => m.screen_clear);
    const svg = svgCanvas(canvas, width, height,
      `${clear.length} of ${data.models.length} models clear all seven deployment checks. Tasks solved runs left to right; unsafe-action rate uses a compressed x^0.4 scale. The shaded region meets the two headline thresholds only.`);
    svg.append(el('rect', {x: x(0.8), y: y(0.01), width: x(1) - x(0.8), height: y(0) - y(0.01), fill: 'var(--good)', opacity: 0.08}));
    yAxis(svg, plot, y, [0, 0.01, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5].filter(v => v <= max), v => pct(v, 0));
    for (const value of ticks(0, 1, 4)) {
      svg.append(text(x(value), plot.bottom + 20, pct(value, 0), {'text-anchor': 'middle', class: 'chart-tick'}));
    }
    svg.append(text(plot.left, 13, 'Unsafe actions · compressed scale', {class: 'chart-axis-title'}),
      text((plot.left + plot.right) / 2, height - 10, 'Tasks solved', {'text-anchor': 'middle', class: 'chart-axis-title'}),
      el('line', {x1: x(0.8), x2: x(0.8), y1: plot.top, y2: plot.bottom, class: 'chart-threshold'}),
      el('line', {x1: plot.left, x2: plot.right, y1: y(0.01), y2: y(0.01), class: 'chart-threshold'}),
      text(x(0.8) - 5, plot.top + 13, '80% tasks', {'text-anchor': 'end', class: 'chart-safety-threshold'}),
      text(plot.left + 5, y(0.01) - 7, '1% unsafe', {class: 'chart-safety-threshold'}));
    if (x(1) - x(0.8) > 140) svg.append(text(x(0.8) + 8, y(0) - 9, 'Meets both thresholds', {class: 'chart-region-label'}));
    const sync = scatter(svg, data.models.map(m => ({model: m, x: x(m.tasks_rate), y: y(m.unsafe_rate),
      color: m.screen_clear ? 'var(--good)' : 'var(--ink-2)', check: m.screen_clear,
      lines: [['Tasks solved', `${pct(m.tasks_rate, 1)} (${int(m.tasks_solved)}/${int(data.suite.tasks)})`],
        ['Unsafe actions', `${pct(m.unsafe_rate, 2)} (${int(m.unsafe_count)})`],
        ['Screen', m.screen_clear ? '✓ Clears all seven checks' : '× Does not clear all seven checks']]})),
    plot, store, tip, root, new Set(clear.map(m => m.id)), {labelMode: 'safety', obstacles: [
      {x: x(0.8) - 62, y: plot.top + 1, width: 60, height: 14},
      {x: plot.left + 3, y: y(0.01) - 17, width: 72, height: 13}
    ]});
    canvas.append(legend);
    return sync;
  });
  const observer = peer ? new ResizeObserver(() => view.redraw()) : null;
  observer?.observe(peer);
  return () => { observer?.disconnect(); view.destroy(); };
}
