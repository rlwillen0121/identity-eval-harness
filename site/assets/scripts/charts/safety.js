import { gSrrPct, int, num, pct } from '../format.js';
import { chart, linear, scatter, svgCanvas, text, yAxis } from './svg.js';

export function mount(root, data, store) {
  const scenarios = data.scenarios ?? data.models?.[0]?.scores?.planned_slots;
  const models = (data.models || []).filter(({ scores }) => scenarios > 0 &&
    Number.isFinite(scores?.g_srr) && Number.isFinite(scores?.og_total) && Number.isFinite(scores?.ab_total));
  const errors = (m) => m.scores.og_total + m.scores.ab_total;
  const rate = (m) => errors(m) / scenarios;
  const scaleForErrors = (maximum) => {
    const rawStep = Math.max(4, maximum) / 4;
    const magnitude = 10 ** Math.floor(Math.log10(rawStep));
    const normalized = rawStep / magnitude;
    const nice = [1, 2, 5, 10].find((value) => value >= normalized) ?? 10;
    const step = Math.max(1, nice * magnitude);
    return { step, max: step * 4 };
  };
  const view = chart(root, 'Clean solve rate vs. total safety errors', `Overgrants and availability breaches summed across all ${int(scenarios ?? 96)} scenarios. Green dots indicate zero errors.`, store, ({ width, canvas, tip }) => {
    if (!models.length) {
      const p = document.createElement('p');
      p.className = 'status-note muted';
      p.textContent = 'No full-suite safety totals have been published.';
      canvas.append(p);
      return () => {};
    }
    const plot = { left: 46, right: width - 18, top: 22, bottom: 260 };
    const maxErrors = Math.max(0, ...models.map(errors));
    const {step, max: yMax} = scaleForErrors(maxErrors);
    const x = linear([0, 1], [plot.left + 8, plot.right - 10]);
    const y = linear([0, yMax], [plot.bottom, plot.top + 20]);
    const svg = svgCanvas(canvas, width, 306, 'Scatter plot of total safety errors across all scenarios against clean solve rate.');
    yAxis(svg, plot, y, Array.from({length: 5}, (_, index) => step * index), int);
    svg.append(text(plot.left, 12, `Total safety errors across ${int(scenarios)} scenarios`, { class: 'chart-axis-title' }));
    for (const v of [0, 0.25, 0.5, 0.75, 1]) {
      svg.append(text(x(v), plot.bottom + 18, pct(v, 0), { 'text-anchor': 'middle', class: 'chart-tick' }));
    }
    svg.append(text((plot.left + plot.right) / 2, 298, 'Clean solve rate', { 'text-anchor': 'middle', class: 'chart-axis-title' }));
    const points = models.map((m) => {
      const total = errors(m);
      return {
        model: m,
        x: x(m.scores.g_srr),
        y: y(total),
        color: total === 0 ? 'var(--good-ink)' : (total >= 20 ? 'var(--critical)' : 'var(--ink-2)'),
        lines: [
          ['Safety score', pct(m.scores?.safety_adjusted_score, 2)],
          ['Clean solve rate', gSrrPct(m.scores?.g_srr)],
          ['Overgrants / breaches', `${int(m.scores?.og_total)} / ${int(m.scores?.ab_total)}`],
          ['Total safety errors', int(total)],
          ['Errors / scenario', num(rate(m), 2)],
        ],
      };
    });
    const labeled = new Set();
    const topClean = models.find((m) => errors(m) === 0);
    if (topClean) labeled.add(topClean.id);
    const highScoreOutlier = models
      .filter((m) => m.scores.g_srr >= 0.7 && rate(m) > 0.5)
      .sort((a, b) => rate(b) - rate(a))[0];
    if (highScoreOutlier) labeled.add(highScoreOutlier.id);
    const worstOutlier = models
      .slice()
      .sort((a, b) => rate(b) - rate(a))[0];
    if (worstOutlier) labeled.add(worstOutlier.id);
    return scatter(svg, points, plot, store, tip, root, labeled, { labelMode: 'safety' });
  });
  return () => view.destroy();
}
