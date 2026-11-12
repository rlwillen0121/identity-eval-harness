import {endToEndPct, int, pct} from '../format.js';
import {chart, el, linear, scatter, svgCanvas, text} from './svg.js';

// A level is one horizontal lane. Sorting makes placement independent of input order.
export function stackDots(models, scale, spacing = 9) {
  const levels = [];
  return models.slice().sort((a, b) => a.g_srr - b.g_srr || a.rank - b.rank || a.id.localeCompare(b.id))
    .map(model => {
      const x = scale(model.g_srr);
      let level = 0;
      while (levels[level]?.some(other => Math.abs(other - x) < spacing)) level++;
      (levels[level] ||= []).push(x);
      return {model, x, level};
    });
}

// Models that resolved nothing share one score, so they are grouped in a compact block beside
// the axis instead of a single tall column. The block fills column by column from the bottom.
export function zeroBlock(models, rows) {
  return models.slice().sort((a, b) => a.rank - b.rank || a.id.localeCompare(b.id))
    .map((model, i) => ({model, column: Math.floor(i / rows), level: i % rows}));
}

export function mount(root, data, store) {
  root.classList.add('chart-field');
  const view = chart(root, null, null, store, ({width, canvas, tip}) => {
    const small = width < 480;
    const radius = small ? 3.5 : 4.5;
    const spacing = small ? 8 : 11;
    const rows = small ? 4 : 3;
    const blockLeft = 12;
    const block = zeroBlock(data.models.filter(m => m.g_srr === 0), rows);
    const blockColumns = Math.max(0, ...block.map(p => p.column + 1));
    const axisLeft = block.length ? blockLeft + (blockColumns - 1) * spacing + (small ? 20 : 36) : blockLeft;
    const x = linear([0, 1], [axisLeft, width - 22]);
    const stacked = stackDots(data.models.filter(m => m.g_srr > 0), x, spacing);
    const levels = Math.max(1, ...stacked.map(p => p.level + 1), ...block.map(p => p.level + 1));
    const baseline = 34 + (levels - 1) * spacing + radius + 7;
    const laneY = level => baseline - radius - 7 - level * spacing;
    const leader = data.models.slice().sort((a, b) => b.g_srr - a.g_srr || a.rank - b.rank)[0];
    const svg = svgCanvas(canvas, width, baseline + 28,
      `Each dot is one of ${int(data.models.length)} models, positioned by end-to-end scenario resolution. ${int(block.length)} resolve none and are grouped at the left; ${leader.name} leads at ${endToEndPct(leader.g_srr)}.`);
    svg.append(el('line', {x1: x(0), x2: x(1), y1: baseline, y2: baseline, class: 'chart-axis'}));
    for (const value of [0, 0.25, 0.5, 0.75, 1]) {
      svg.append(el('line', {x1: x(value), x2: x(value), y1: baseline, y2: baseline + 4, class: 'chart-axis'}),
        text(x(value), baseline + 20, pct(value, 0), {class: 'chart-tick', 'text-anchor': 'middle'}));
    }
    const obstacles = [];
    if (block.length) {
      const label = `${int(block.length)} resolve none`;
      const labelY = laneY(levels - 1) - radius - 8;
      svg.append(text(blockLeft - radius, labelY, label, {class: 'chart-field-count'}));
      obstacles.push({x: blockLeft - radius, y: labelY - 12, width: label.length * 7.2, height: 16});
    }
    const mark = (model) => ({radius, ring: 'var(--page)', strokeWidth: 1.5,
      color: model.id === leader.id ? 'var(--accent)' : 'var(--ink-2)',
      lines: [['End to end', endToEndPct(model.g_srr)], ['Rank', `#${int(model.rank)}`]]});
    const points = [
      ...block.map(p => ({...p, x: blockLeft + p.column * spacing, y: laneY(p.level), ...mark(p.model)})),
      ...stacked.map(p => ({...p, y: laneY(p.level), ...mark(p.model)})),
    ];
    return scatter(svg, points, {left: 0, right: width, top: 0, bottom: baseline - 5},
      store, tip, root, new Set([leader.id]), {obstacles});
  });
  const caption = document.createElement('p');
  caption.className = 'chart-caption';
  caption.textContent = 'Each dot is one model, placed by the share of scenarios it resolved end to end.';
  root.append(caption);
  return () => view.destroy();
}
