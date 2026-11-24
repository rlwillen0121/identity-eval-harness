import { gSrrPct, int, num, pct } from '../format.js';
import { chart, linear, scatter, svgCanvas, text, ticks, yAxis } from './svg.js';

export function mount(root, data, store) {
  const models = data.models || [];
  const view = chart(root, 'Safety violations vs. commit rate', 'Overgrants and availability breaches per committed deployment against commit rate.', store, ({ width, canvas, tip }) => {
    if (!models.length) {
      const p = document.createElement('p');
      p.className = 'status-note muted';
      p.textContent = 'Awaiting ranked v5.2 entries to plot safety violations.';
      canvas.append(p);
      return () => {};
    }
    const plot = { left: 46, right: width - 18, top: 22, bottom: 260 };
    const maxViol = Math.max(1, ...models.map((m) => m.scores?.violations_per_committed_slot || 0));
    const x = linear([0, 1], [plot.left + 8, plot.right - 10]);
    const y = linear([0, maxViol], [plot.bottom, plot.top + 20]);
    const svg = svgCanvas(canvas, width, 306, 'Scatter plot of safety violations per committed scenario against commit rate.');
    yAxis(svg, plot, y, ticks(0, maxViol, 4), (v) => num(v, 1));
    svg.append(text(plot.left, 12, '(OG + AB) / committed', { class: 'chart-axis-title' }));
    for (const v of [0, 0.25, 0.5, 0.75, 1]) {
      svg.append(text(x(v), plot.bottom + 18, pct(v, 0), { 'text-anchor': 'middle', class: 'chart-tick' }));
    }
    svg.append(text((plot.left + plot.right) / 2, 298, 'Commit rate', { 'text-anchor': 'middle', class: 'chart-axis-title' }));
    const points = models.map((m) => ({
      model: m,
      x: x(m.scores?.commit_rate ?? 0),
      y: y(m.scores?.violations_per_committed_slot ?? 0),
      color: (m.scores?.og_total || 0) + (m.scores?.ab_total || 0) === 0 ? 'var(--good-ink)' : 'var(--ink-2)',
      lines: [
        ['G-SRR', gSrrPct(m.scores?.g_srr)],
        ['Commit rate', pct(m.scores?.commit_rate, 1)],
        ['OG / AB', `${int(m.scores?.og_total)} / ${int(m.scores?.ab_total)}`],
        ['(OG+AB)/commit', num(m.scores?.violations_per_committed_slot, 2)],
      ],
    }));
    return scatter(svg, points, plot, store, tip, root, new Set(models.slice(0, 3).map((m) => m.id)), { labelMode: 'safety' });
  });
  return () => view.destroy();
}
