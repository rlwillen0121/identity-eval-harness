import { gSrrPct, pct, usd } from '../format.js';
import { chart, el, log, linear, scatter, stepObstacles, stepPath, svgCanvas, text, ticks, yAxis } from './svg.js';

export function paretoFrontier(models, metric = 'safety_adjusted_score') {
  const valueOf = (m) => m.scores?.[metric];
  const paid = models
    .filter((m) => (m.usage?.estimated_cost_usd ?? 0) > 0 && Number.isFinite(valueOf(m)))
    .slice()
    .sort((a, b) => a.usage.estimated_cost_usd - b.usage.estimated_cost_usd || valueOf(b) - valueOf(a) || a.rank - b.rank);
  let best = -Infinity;
  return paid.filter((m) => {
    const val = valueOf(m);
    if (val <= best) return false;
    best = val;
    return true;
  });
}

export function mount(root, data, store) {
  const models = data.models || [];
  let metric = 'safety_adjusted_score';
  const view = chart(root, 'Score vs. estimated cost', 'Higher and further left is better. The blue line traces the best score among plotted runs at each budget.', store, ({ width, canvas, tip }) => {
    const measured = models.filter((m) => Number.isFinite(m.scores?.[metric]) && Number.isFinite(m.usage?.estimated_cost_usd));
    const priced = measured.filter((m) => m.usage.estimated_cost_usd > 0);
    const free = measured.filter((m) => m.usage.estimated_cost_usd === 0);
    if (free.length) {
      const note = document.createElement('p');
      note.className = 'chart-sub';
      note.textContent = `The logarithmic cost axis excludes $0 estimates for ${free.map((m) => m.name).join(', ')}.`;
      canvas.append(note);
    }
    if (!priced.length) {
      if (!free.length) {
        const p = document.createElement('p');
        p.className = 'status-note muted';
        p.textContent = 'No full-suite configurations have a published cost and score for this metric.';
        canvas.append(p);
      }
      return () => {};
    }
    const plot = { left: 46, right: width - 18, top: 22, bottom: 300 };
    const costs = priced.map((m) => m.usage.estimated_cost_usd);
    const minCost = Math.min(1, ...costs);
    const maxCost = Math.max(100, ...costs) * 1.15;
    const x = log([minCost, maxCost], [plot.left + 8, plot.right - 8]);
    const minScore = Math.min(0, ...priced.map((m) => Math.floor(4 * m.scores[metric]) / 4));
    const y = linear([minScore, 1], [plot.bottom, plot.top + 24]);
    const frontier = paretoFrontier(priced, metric);
    const svg = svgCanvas(canvas, width, 352, `Score vs. estimated cost across ${priced.length} priced models.`);
    yAxis(svg, plot, y, ticks(minScore, 1, Math.min(8, (1 - minScore) * 4)), (v) => pct(v, 0));
    const axisLabel = metric === 'safety_adjusted_score' ? 'Safety score' : (metric === 'g_srr' ? 'End-to-end score' : 'Domain avg');
    svg.append(text(plot.left, 12, axisLabel, { class: 'chart-axis-title' }));
    for (const v of [1, 3, 10, 30, 100, 300]) {
      if (v >= minCost && v <= maxCost) {
        svg.append(
          el('line', { x1: x(v), x2: x(v), y1: plot.bottom, y2: plot.bottom + 5, class: 'chart-axis' }),
          text(x(v), plot.bottom + 20, `$${v}`, { 'text-anchor': 'middle', class: 'chart-tick' }),
        );
      }
    }
    svg.append(text((plot.left + plot.right) / 2, 342, 'Estimated run cost · USD (log scale)', { 'text-anchor': 'middle', class: 'chart-axis-title' }));
    const frontierPoints = frontier.map((m) => ({ x: x(m.usage.estimated_cost_usd), y: y(m.scores[metric]) }));
    const line = el('path', { d: stepPath(frontierPoints, plot.right), class: 'chart-frontier', stroke: 'var(--accent)' });
    svg.append(line);
    const ids = new Set(frontier.map((m) => m.id));
    const points = priced.map((m) => ({
      model: m,
      x: x(m.usage.estimated_cost_usd),
      y: y(m.scores[metric]),
      color: ids.has(m.id) ? 'var(--accent)' : 'var(--ink-2)',
      opacity: ids.has(m.id) ? 1 : 0.65,
      lines: [
        ['Safety score', pct(m.scores?.safety_adjusted_score, 2)],
        ['End to end', gSrrPct(m.scores?.g_srr)],
        ...(Number.isFinite(m.scores?.macro_cat_acc) ? [['Domain avg', pct(m.scores.macro_cat_acc, 1)]] : []),
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
  control.setAttribute('aria-label', 'Score metric');
  const buttons = [['safety_adjusted_score', 'Score'], ['g_srr', 'End to end'], ['macro_cat_acc', 'Domain avg']].map(([key, label]) => {
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
