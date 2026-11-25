import { gSrrPct, int, num, pct } from '../format.js';
import { chart, linear, scatter, svgCanvas, text, ticks, yAxis } from './svg.js';

export function mount(root, data, store) {
  const models = data.models || [];
  const view = chart(root, 'Score vs. safety errors', 'Overgrants and broken access per deployment against end-to-end score. Bottom-right in green is ideal (high score, zero safety errors).', store, ({ width, canvas, tip }) => {
    if (!models.length) {
      const p = document.createElement('p');
      p.className = 'status-note muted';
      p.textContent = 'Awaiting completed runs to plot safety errors.';
      canvas.append(p);
      return () => {};
    }
    const plot = { left: 46, right: width - 18, top: 22, bottom: 260 };
    const maxViol = Math.max(1, ...models.map((m) => m.scores?.violations_per_committed_slot || 0));
    const x = linear([0, 1], [plot.left + 8, plot.right - 10]);
    const y = linear([0, maxViol], [plot.bottom, plot.top + 20]);
    const svg = svgCanvas(canvas, width, 306, 'Scatter plot of safety errors per scenario against end-to-end score.');
    yAxis(svg, plot, y, ticks(0, maxViol, 4), (v) => num(v, 1));
    svg.append(text(plot.left, 12, 'Safety errors / scenario', { class: 'chart-axis-title' }));
    for (const v of [0, 0.25, 0.5, 0.75, 1]) {
      svg.append(text(x(v), plot.bottom + 18, pct(v, 0), { 'text-anchor': 'middle', class: 'chart-tick' }));
    }
    svg.append(text((plot.left + plot.right) / 2, 298, 'End-to-end score', { 'text-anchor': 'middle', class: 'chart-axis-title' }));
    const points = models.map((m) => {
      const total = (m.scores?.og_total || 0) + (m.scores?.ab_total || 0);
      return {
        model: m,
        x: x(m.scores?.g_srr ?? 0),
        y: y(m.scores?.violations_per_committed_slot ?? 0),
        color: total === 0 ? 'var(--good-ink)' : (total >= 20 ? 'var(--critical)' : 'var(--ink-2)'),
        lines: [
          ['End to end', gSrrPct(m.scores?.g_srr)],
          ['Overgrants / broken', `${int(m.scores?.og_total)} / ${int(m.scores?.ab_total)}`],
          ['Errors / scenario', num(m.scores?.violations_per_committed_slot, 2)],
        ],
      };
    });
    const labeled = new Set(models.slice(0, 4).map((m) => m.id));
    return scatter(svg, points, plot, store, tip, root, labeled, { labelMode: 'safety' });
  });
  return () => view.destroy();
}
