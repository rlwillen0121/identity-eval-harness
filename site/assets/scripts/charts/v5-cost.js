import { endToEndPct, pct, usd } from '../format.js';
import { paretoFrontier } from './frontier.js';
import { chart, el, linear, log, scatter, stepObstacles, stepPath, svgCanvas, text, ticks, yAxis } from './svg.js';

export function mount(root, data, store) {
  let metric = 'g_srr';
  const subtitle = 'Run cost on a log scale across the 18 protocol-complete runs.';
  const view = chart(root, 'Resolution vs. run cost', subtitle, store, ({ width, canvas, tip }) => {
    const plot = { left: 44, right: width - 18, top: 22, bottom: 312 };
    const paid = data.models.filter((m) => m.cost_basis !== 'local' && Number(m.cost_usd) > 0);
    const local = data.models.filter((m) => m.cost_basis === 'local');
    const logStart = plot.left + (local.length ? 52 : 0);
    const minCost = Math.min(10, ...paid.map((m) => m.cost_usd));
    const maxCost = Math.max(500, ...paid.map((m) => m.cost_usd)) * 1.15;
    const x = log([minCost, maxCost], [logStart, plot.right - 6]);
    const y = linear([0, 1], [plot.bottom, plot.top + 26]);
    const frontier = paretoFrontier(data.models, metric);
    const lead = data.models.reduce((a, b) => (a[metric] > b[metric] ? a : b));
    const metricLabel = metric === 'g_srr' ? 'G-SRR' : 'Sem-SRR@commit';
    const leadRate = metric === 'g_srr' ? endToEndPct(lead[metric]) : pct(lead[metric], 1);
    const svg = svgCanvas(canvas, width, 366,
      `${lead.name} has the highest ${metricLabel} at ${leadRate}; ${frontier.length} paid configurations form the cost frontier.`);
    yAxis(svg, plot, y, ticks(0, 1, 4), (v) => pct(v, 0));
    svg.append(text(plot.left, 12, metricLabel, { class: 'chart-axis-title' }));
    for (const value of [10, 30, 100, 300, 600]) {
      if (value < minCost || value > maxCost) continue;
      svg.append(
        el('line', { x1: x(value), x2: x(value), y1: plot.bottom, y2: plot.bottom + 5, class: 'chart-axis' }),
        text(x(value), plot.bottom + 20, `$${value}`, { 'text-anchor': 'middle', class: 'chart-tick' }),
      );
    }
    if (local.length) {
      const divider = logStart - 18;
      svg.append(
        el('line', { x1: divider, x2: divider, y1: plot.top, y2: plot.bottom, class: 'chart-axis' }),
        text(plot.left + 12, plot.bottom + 20, 'local', { 'text-anchor': 'middle', class: 'chart-tick' }),
      );
    }
    svg.append(text((logStart + plot.right) / 2, 356, 'Run cost · USD (144 slots)', { 'text-anchor': 'middle', class: 'chart-axis-title' }));
    const frontierPoints = frontier.map((m) => ({ x: x(m.cost_usd), y: y(m[metric]) }));
    const line = el('path', { d: stepPath(frontierPoints, plot.right), class: 'chart-frontier' });
    svg.append(line);
    const ids = new Set(frontier.map((m) => m.id));
    const points = data.models.map((m) => ({
      model: m,
      x: m.cost_basis === 'local'
        ? plot.left + 12 + (local.indexOf(m) - (local.length - 1) / 2) * 12
        : Number(m.cost_usd) > 0 ? x(m.cost_usd) : logStart,
      y: y(m[metric]),
      color: ids.has(m.id) ? 'var(--accent)' : 'var(--ink-2)',
      opacity: m[metric] === 0 ? 0.35 : ids.has(m.id) ? 1 : 0.6,
      radius: m[metric] === 0 ? 3 : undefined,
      lines: [
        ['Run cost', m.cost_basis === 'local' ? 'No run cost (local hardware)' : `${usd(m.cost_usd)} (${m.cost_basis})`],
        ['G-SRR', endToEndPct(m.g_srr)],
        ['Sem-SRR@commit', pct(m.sem_srr_commit)],
      ],
    }));
    const sync = scatter(svg, points, plot, store, tip, root, ids, {
      labelMode: 'frontier',
      obstacles: stepObstacles(frontierPoints, plot.right),
    });
    return (state) => {
      line.setAttribute('stroke', 'var(--accent)');
      line.setAttribute('opacity', state.hover ? 0.35 : 1);
      sync(state);
    };
  });
  const control = document.createElement('div');
  control.className = 'chart-segmented';
  control.setAttribute('role', 'group');
  control.setAttribute('aria-label', 'Resolution metric');
  const buttons = [['g_srr', 'G-SRR'], ['sem_srr_commit', 'Sem-SRR@commit']].map(([key, label]) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = label;
    button.setAttribute('aria-pressed', key === metric ? 'true' : 'false');
    button.addEventListener('click', () => {
      metric = key;
      for (const [i, item] of buttons.entries()) item.setAttribute('aria-pressed', ['g_srr', 'sem_srr_commit'][i] === metric ? 'true' : 'false');
      view.redraw();
    });
    control.append(button);
    return button;
  });
  view.header.append(control);
  return () => view.destroy();
}
