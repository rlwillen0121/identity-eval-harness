import { endToEndPct, int, pct } from '../format.js';
import { chart, el, power, scatter, svgCanvas, text, yAxis } from './svg.js';

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

// Both axes are the share of episodes with at least one committed violation of that kind.
export const SAFE_ZONE = 0.05;

export function safetyRates(model) {
  const slots = Number(model.planned_slots) || 0;
  return {
    og: slots ? (Number(model.overgrant_slots) || 0) / slots : 0,
    ab: slots ? (Number(model.avail_breach_slots) || 0) / slots : 0,
  };
}

export function mount(root, data, store) {
  const slots = Number(data.suite?.slots_per_run);
  const subtitle = `Share of the ${int(slots)} episodes with at least one committed violation of each kind.`;
  const legend = document.createElement('p');
  legend.className = 'chart-legend';
  legend.textContent = `Shaded: violations in ≤ ${pct(SAFE_ZONE, 0)} of episodes on both axes · Compressed scales`;
  const peer = outcomesPeer(root);
  const view = chart(root, 'Overgrants vs. availability breaches', subtitle, store, ({ width, canvas, tip }) => {
    canvas.append(legend);
    const height = plotHeight(root, peer);
    const plot = { left: 48, right: width - 18, top: 26, bottom: height - 54 };
    const x = power([0, 1], [plot.left + 6, plot.right - 6], 0.6);
    const y = power([0, 1], [plot.bottom, plot.top], 0.45);
    const lowRisk = data.models.filter((m) => {
      const { og, ab } = safetyRates(m);
      return m.g_srr > 0 || (og <= SAFE_ZONE && ab <= SAFE_ZONE);
    });
    const svg = svgCanvas(canvas, width, height,
      `Scatter plot of the share of episodes with overgrants vs. availability breaches across ${int(data.models.length)} ranked models.`);
    svg.append(el('rect', {
      x: x(0),
      y: y(SAFE_ZONE),
      width: Math.max(0, x(SAFE_ZONE) - x(0)),
      height: Math.max(0, y(0) - y(SAFE_ZONE)),
      fill: 'var(--good)',
      opacity: 0.08,
    }));
    yAxis(svg, plot, y, [0, SAFE_ZONE, 0.25, 0.5, 1], (v) => pct(v, 0));
    for (const value of [0, SAFE_ZONE, 0.25, 0.5, 1]) {
      svg.append(text(x(value), plot.bottom + 20, pct(value, 0), { 'text-anchor': 'middle', class: 'chart-tick' }));
    }
    svg.append(
      text(plot.left, 13, 'Episodes with overgrants', { class: 'chart-axis-title' }),
      text((plot.left + plot.right) / 2, height - 10, 'Episodes with availability breaches', { 'text-anchor': 'middle', class: 'chart-axis-title' }),
      el('line', { x1: x(SAFE_ZONE), x2: x(SAFE_ZONE), y1: plot.top, y2: plot.bottom, class: 'chart-threshold' }),
      el('line', { x1: plot.left, x2: plot.right, y1: y(SAFE_ZONE), y2: y(SAFE_ZONE), class: 'chart-threshold' }),
    );
    const labelIds = new Set(lowRisk.map((m) => m.id));
    const sync = scatter(svg, data.models.map((m) => {
      const { og, ab } = safetyRates(m);
      const inLowZone = og <= SAFE_ZONE && ab <= SAFE_ZONE;
      return {
        model: m,
        x: x(ab),
        y: y(og),
        color: inLowZone ? 'var(--good)' : m.overgrant_count === 0 && m.g_srr > 0 ? 'var(--accent)' : 'var(--ink-2)',
        lines: [
          ['Overgrants', `${int(m.overgrant_count)} in ${int(m.overgrant_slots)} episodes (${pct(og, 1)})`],
          ['Availability breaches', `${int(m.avail_breach_count)} in ${int(m.avail_breach_slots)} episodes (${pct(ab, 1)})`],
          ['Any violation', `${int(m.unsafe_slots)} of ${int(m.planned_slots)} episodes`],
          ['End to end', endToEndPct(m.g_srr)],
        ],
      };
    }), plot, store, tip, root, labelIds, { labelMode: 'safety' });
    canvas.append(legend);
    return sync;
  });
  const observer = peer ? new ResizeObserver(() => view.redraw()) : null;
  observer?.observe(peer);
  return () => { observer?.disconnect(); view.destroy(); };
}
