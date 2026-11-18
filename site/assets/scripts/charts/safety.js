import { endToEndPct, int, pct } from '../format.js';
import { chart, el, linear, power, scatter, svgCanvas, text, ticks, yAxis } from './svg.js';

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
  const subtitle = 'Committed overgrant and availability-breach means per slot across 144 slots.';
  const legend = document.createElement('p');
  legend.className = 'chart-legend';
  legend.textContent = 'Shaded region: overgrant ≤ 5% and availability breach ≤ 20% · Compressed scale on overgrants';
  const peer = outcomesPeer(root);
  const view = chart(root, 'Overgrant vs. availability breaches', subtitle, store, ({ width, canvas, tip }) => {
    canvas.append(legend);
    const height = plotHeight(root, peer);
    const plot = { left: 48, right: width - 18, top: 26, bottom: height - 54 };
    const maxAB = Math.max(1, Math.ceil(Math.max(...data.models.map((m) => m.avail_breach_rate)) * 10) / 10);
    const maxOG = Math.max(0.5, Math.ceil(Math.max(...data.models.map((m) => m.overgrant_rate)) * 2) / 2);
    const x = linear([0, maxAB], [plot.left + 6, plot.right - 6]);
    const y = power([0, maxOG], [plot.bottom, plot.top], 0.45);
    const lowBreach = data.models.filter((m) => m.g_srr >= 0.35 || (m.overgrant_count === 0 && m.g_srr > 0));
    const svg = svgCanvas(canvas, width, height,
      `Scatter plot of committed overgrant mean vs. availability-breach mean across ${int(data.models.length)} complete models.`);
    svg.append(el('rect', {
      x: x(0),
      y: y(0.05),
      width: Math.max(0, x(0.2) - x(0)),
      height: Math.max(0, y(0) - y(0.05)),
      fill: 'var(--good)',
      opacity: 0.08,
    }));
    yAxis(svg, plot, y, [0, 0.05, 0.2, 0.5, 1.0, 2.0, 2.5].filter((v) => v <= maxOG), (v) => pct(v, 0));
    for (const value of ticks(0, maxAB, 4)) {
      svg.append(text(x(value), plot.bottom + 20, pct(value, 0), { 'text-anchor': 'middle', class: 'chart-tick' }));
    }
    svg.append(
      text(plot.left, 13, 'Overgrants (OG) · compressed scale', { class: 'chart-axis-title' }),
      text((plot.left + plot.right) / 2, height - 10, 'Availability breaches (AB)', { 'text-anchor': 'middle', class: 'chart-axis-title' }),
      el('line', { x1: x(0.2), x2: x(0.2), y1: plot.top, y2: plot.bottom, class: 'chart-threshold' }),
      el('line', { x1: plot.left, x2: plot.right, y1: y(0.05), y2: y(0.05), class: 'chart-threshold' }),
      text(x(0.2) + 5, plot.top + 13, '20% AB', { class: 'chart-safety-threshold' }),
      text(plot.right - 4, y(0.05) - 7, '5% OG', { 'text-anchor': 'end', class: 'chart-safety-threshold' }),
    );
    const labelIds = new Set(lowBreach.map((m) => m.id));
    const sync = scatter(svg, data.models.map((m) => {
      const zeroOgResolving = m.overgrant_count === 0 && m.g_srr > 0;
      const inLowZone = m.overgrant_rate <= 0.05 && m.avail_breach_rate <= 0.20 && m.g_srr > 0;
      return {
        model: m,
        x: x(m.avail_breach_rate),
        y: y(m.overgrant_rate),
        color: inLowZone ? 'var(--good)' : zeroOgResolving ? 'var(--accent)' : 'var(--ink-2)',
        lines: [
          ['OG', `${pct(m.overgrant_rate, 1)} (${int(m.overgrant_count)})`],
          ['AB', `${pct(m.avail_breach_rate, 1)} (${int(m.avail_breach_count)})`],
          ['End to end', endToEndPct(m.g_srr)],
          ['State accuracy', pct(m.sem_srr_commit, 1)],
        ],
      };
    }), plot, store, tip, root, labelIds, {
      labelMode: 'safety',
      obstacles: [
        { x: x(0.2) + 3, y: plot.top + 1, width: 48, height: 14 },
        { x: plot.right - 50, y: y(0.05) - 17, width: 48, height: 13 },
      ],
    });
    canvas.append(legend);
    return sync;
  });
  const observer = peer ? new ResizeObserver(() => view.redraw()) : null;
  observer?.observe(peer);
  return () => { observer?.disconnect(); view.destroy(); };
}
