import {int, pct} from '../format.js';
import {chart, linear, scatter, stepPath, svgCanvas, text, ticks, yAxis, el} from './svg.js';

const day = 86400000;
const stamp = date => Date.parse(`${date}T00:00:00Z`);
const dateLabel = date => new Date(date).toLocaleDateString('en-US', {month: 'short', day: 'numeric', timeZone: 'UTC'});

export function mount(root, data, store) {
  const models = data.models.filter(m => m.first_published !== null);
  const omitted = data.models.length - models.length;
  const subtitle = 'Each dot is a model on the date its completed 180-scenario run was published.' +
    (omitted ? ` ${omitted} model${omitted === 1 ? '' : 's'} without a publication date omitted.` : '');
  const view = chart(root, 'The leaderboard over time', subtitle, store, ({width, canvas, tip}) => {
    const plot = {left: 44, right: width - 18, top: 24, bottom: 300};
    const dates = data.history.map(h => stamp(h.date)).filter(Number.isFinite);
    const start = dates.length ? Math.min(...dates) : 0;
    const end = dates.length ? Math.max(...dates) : day;
    const x = linear([start, end], [plot.left + 12, plot.right - 12]);
    const y = linear([0, 1], [plot.bottom, plot.top]);
    const daily = new Map();
    for (const model of models) {
      const date = model.first_published;
      if (!daily.has(date) || daily.get(date).g_srr < model.g_srr) daily.set(date, model);
    }
    let best = -1;
    const records = [...daily.entries()].sort(([a], [b]) => a.localeCompare(b)).filter(([, model]) => {
      if (model.g_srr <= best) return false;
      best = model.g_srr;
      return true;
    }).map(([date, model]) => ({x: x(stamp(date)), y: y(model.g_srr), model}));
    const leader = records.at(-1)?.model;
    const svg = svgCanvas(canvas, width, 354, leader ?
      `${models.length} published models span ${dateLabel(start)} to ${dateLabel(end)}. ${leader.name} sets the running best of ${pct(leader.g_srr, 1)}. Horizontal nudges separate same-day marks.` : 'No models have publication dates.');
    yAxis(svg, plot, y, ticks(0, 1, 4), v => pct(v, 0));
    svg.append(text(plot.left, 12, 'End to end', {class: 'chart-axis-title'}));
    const span = Math.max(1, (end - start) / day);
    const tickStep = Math.max(1, Math.ceil(span / Math.max(1, Math.floor((plot.right - plot.left) / 85))));
    const dateTicks = [];
    for (let t = start; t <= end; t += tickStep * day) dateTicks.push(t);
    if (end - dateTicks.at(-1) >= tickStep * day * 0.7) dateTicks.push(end);
    for (const t of dateTicks) svg.append(text(x(t), 320, dateLabel(t), {'text-anchor': 'middle', class: 'chart-tick'}));
    svg.append(text((plot.left + plot.right) / 2, 344, 'Date published', {'text-anchor': 'middle', class: 'chart-axis-title'}));
    const line = el('path', {d: stepPath(records, plot.right), class: 'chart-frontier'});
    svg.append(line);
    const points = [];
    const byDate = new Map();
    for (const model of models) {
      if (!byDate.has(model.first_published)) byDate.set(model.first_published, []);
      byDate.get(model.first_published).push(model);
    }
    const recordIds = new Set(records.map(p => p.model.id));
    const firstDate = models.map(m => m.first_published).sort()[0];
    const labelIds = new Set([...recordIds, ...models.filter(m => m.first_published > firstDate && m.g_srr > 0.5).map(m => m.id)]);
    const offsets = [0, ...Array.from({length: 22}, (_, i) => [-(i + 1), i + 1]).flat()];
    for (const [date, peers] of [...byDate.entries()].sort(([a], [b]) => a.localeCompare(b))) {
      const target = x(stamp(date));
      const clustered = [];
      const ordered = peers.slice().sort((a, b) => y(a.g_srr) - y(b.g_srr) || a.name.localeCompare(b.name) || a.id.localeCompare(b.id));
      for (const model of ordered) {
        const py = y(model.g_srr);
        const distanceScore = offset => {
          const distances = clustered.map(p => Math.hypot(offset - p.offset, py - p.y));
          return {min: distances.length ? Math.min(...distances) : Infinity,
            sum: distances.reduce((total, distance) => total + Math.min(distance, 36), 0)};
        };
        const feasible = offsets.find(offset => distanceScore(offset).min >= 9);
        const offset = feasible ?? offsets.slice().sort((a, b) => {
          const sa = distanceScore(a);
          const sb = distanceScore(b);
          return sb.min - sa.min || sb.sum - sa.sum || Math.abs(a) - Math.abs(b) || a - b;
        })[0];
        clustered.push({model, offset, y: py});
        points.push({model, x: target + offset, y: py,
          color: recordIds.has(model.id) ? 'var(--accent)' : 'var(--ink-2)',
          opacity: model.g_srr === 0 ? 0.35 : 0.7, radius: model.g_srr === 0 ? 3 : undefined,
          lines: [['Date added', dateLabel(stamp(model.first_published)) + `, ${model.first_published.slice(0, 4)}`],
            ['End to end', pct(model.g_srr, 1)], ['Rank today', `#${int(model.rank)}`]]});
      }
    }
    const sync = scatter(svg, points, plot, store, tip, root, labelIds);
    return state => { line.setAttribute('stroke', state.hover ? 'var(--axis)' : 'var(--accent)'); sync(state); };
  });
  return () => view.destroy();
}
