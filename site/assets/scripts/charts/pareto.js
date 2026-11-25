import { gSrrPct, pct, usd } from '../format.js';
import { chart, el, log, linear, scatter, stepObstacles, stepPath, svgCanvas, text, ticks, yAxis } from './svg.js';

export function paretoFrontier(models, metric = 'g_srr') {
  const paid = models
    .filter((m) => (m.usage?.estimated_cost_usd ?? 0) > 0 && (m.scores?.[metric] ?? 0) > 0)
    .slice()
    .sort((a, b) => a.usage.estimated_cost_usd - b.usage.estimated_cost_usd || b.scores[metric] - a.scores[metric] || a.rank - b.rank);
  let best = 0;
  return paid.filter((m) => {
    const val = m.scores[metric];
    if (val <= best) return false;
    best = val;
    return true;
  });
}

export function mount(root, data, store) {
  const models = data.models || [];
  let metric = 'g_srr';
  const view = chart(root, 'Accuracy vs. estimated cost (Pareto)', 'Side metric view under shared frozen retail pricing; cost never affects leaderboard ranking.', store, ({ width, canvas, tip }) => {
    if (!models.length) {
      const p = document.createElement('p');
      p.className = 'status-note muted';
      p.textContent = 'Awaiting ranked v5.3 entries to plot the accuracy–cost Pareto frontier.';
      canvas.append(p);
      return () => {};
    }
    const plot = { left: 46, right: width - 18, top: 22, bottom: 300 };
    const costs = models.map((m) => m.usage?.estimated_cost_usd || 0).filter((c) => c > 0);
    const minCost = Math.min(1, ...costs);
    const maxCost = Math.max(100, ...costs) * 1.15;
    const x = log([minCost, maxCost], [plot.left + 8, plot.right - 8]);
    const y = linear([0, 1], [plot.bottom, plot.top + 24]);
    const frontier = paretoFrontier(models, metric);
    const svg = svgCanvas(canvas, width, 352, `Accuracy-cost Pareto chart across ${models.length} ranked v5.3 entries.`);
    yAxis(svg, plot, y, ticks(0, 1, 4), (v) => pct(v, 0));
    svg.append(text(plot.left, 12, metric === 'g_srr' ? 'G-SRR' : 'Macro-Cat-Acc', { class: 'chart-axis-title' }));
    for (const v of [1, 3, 10, 30, 100, 300]) {
      if (v >= minCost && v <= maxCost) {
        svg.append(
          el('line', { x1: x(v), x2: x(v), y1: plot.bottom, y2: plot.bottom + 5, class: 'chart-axis' }),
          text(x(v), plot.bottom + 20, `$${v}`, { 'text-anchor': 'middle', class: 'chart-tick' }),
        );
      }
    }
    svg.append(text((plot.left + plot.right) / 2, 342, 'Estimated run cost · USD (log scale, report-only)', { 'text-anchor': 'middle', class: 'chart-axis-title' }));
    const frontierPoints = frontier.map((m) => ({ x: x(m.usage.estimated_cost_usd), y: y(m.scores[metric]) }));
    const line = el('path', { d: stepPath(frontierPoints, plot.right), class: 'chart-frontier', stroke: 'var(--accent)' });
    svg.append(line);
    const ids = new Set(frontier.map((m) => m.id));
    const points = models.map((m) => ({
      model: m,
      x: (m.usage?.estimated_cost_usd || 0) > 0 ? x(m.usage.estimated_cost_usd) : plot.left + 12,
      y: y(m.scores?.[metric] ?? 0),
      color: ids.has(m.id) ? 'var(--accent)' : 'var(--ink-2)',
      opacity: ids.has(m.id) ? 1 : 0.65,
      lines: [
        ['G-SRR', gSrrPct(m.scores?.g_srr)],
        ['Macro-Cat-Acc', pct(m.scores?.macro_cat_acc, 1)],
        ['Est. cost', usd(m.usage?.estimated_cost_usd)],
      ],
    }));
    const sync = scatter(svg, points, plot, store, tip, root, ids, { labelMode: 'frontier', obstacles: stepObstacles(frontierPoints, plot.right) });
    return (state) => {
      line.setAttribute('opacity', state.hover ? 0.35 : 1);
      sync(state);
    };
  });
  const control = document.createElement('div');
  control.className = 'chart-segmented';
  control.setAttribute('role', 'group');
  control.setAttribute('aria-label', 'Accuracy metric');
  const buttons = [['g_srr', 'G-SRR'], ['macro_cat_acc', 'Macro-Cat-Acc']].map(([key, label]) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = label;
    btn.setAttribute('aria-pressed', String(key === metric));
    btn.addEventListener('click', () => {
      metric = key;
      buttons.forEach(([b, k]) => b.setAttribute('aria-pressed', String(k === metric)));
      view.redraw();
    });
    control.append(btn);
    return [btn, key];
  });
  view.header.append(control);
  return () => view.destroy();
}
