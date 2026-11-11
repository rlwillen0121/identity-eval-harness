import {int, pct} from '../format.js';
import {bindMark, chart, el, markPosition, modelRow, nameColumn, svgCanvas, text} from './svg.js';

const median = values => {
  const sorted = values.slice().sort((a, b) => a - b);
  const i = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[i] : (sorted[i - 1] + sorted[i]) / 2;
};

export const shade = rate => rate === 0 ? 'var(--seq-0)' : `var(--seq-${Math.min(7, Math.ceil(rate * 7))})`;
const wrapLabel = (label, maxWidth) => {
  const words = label.split(/\s+/u);
  if (words.length < 2) return [label];
  let best = [words[0], words.slice(1).join(' ')];
  let bestScore = Infinity;
  for (let split = 1; split < words.length; split++) {
    const lines = [words.slice(0, split).join(' '), words.slice(split).join(' ')];
    const widest = Math.max(...lines.map(line => line.length * 5.1));
    const overflow = Math.max(0, widest - maxWidth);
    const score = overflow * 100 + Math.abs(lines[0].length - lines[1].length);
    if (score < bestScore) { best = lines; bestScore = score; }
  }
  return best;
};

export function mount(root, data, store) {
  const subtitle = 'Pass rate by category and optimisation problem; stronger shading indicates higher rates.';
  const models = data.models.slice().sort((a, b) => a.rank - b.rank);
  const medians = data.categories.map(c => ({...c, rate: median(models.map(m => m.categories[c.id] / data.suite.scenarios))}))
    .sort((a, b) => a.rate - b.rate);
  const view = chart(root, 'Where models fail', subtitle, store, ({width, canvas, sub, tip}) => {
    const mobile = width < 640;
    sub.textContent = subtitle + (mobile ? ' Optimisation columns are hidden at this width.' : '');
    const columns = data.categories.map(c => ({...c, category: true})).concat(mobile ? [] : data.opt_tasks);
    const left = nameColumn(width, models);
    const gap = mobile ? 0 : 14;
    const cellWidth = Math.max(1, (width - left - 4 - gap) / columns.length);
    const top = 64;
    const height = top + models.length * 20 + 8;
    const svg = svgCanvas(canvas, width, height,
      `${models.length} model rows compare ${data.categories.length} categories${mobile ? '' : ` and ${data.opt_tasks.length} optimisation problems`}. ${medians[0].label} has the lowest median category pass rate at ${pct(medians[0].rate, 1)}; ${medians.at(-1).label} has the highest at ${pct(medians.at(-1).rate, 1)}.`);
    const cx = i => left + i * cellWidth + (!mobile && i >= data.categories.length ? gap : 0);
    svg.append(text(left + data.categories.length * cellWidth / 2, 13, 'Categories',
      {class: 'chart-column-group', 'text-anchor': 'middle'}));
    if (!mobile) svg.append(text(left + data.categories.length * cellWidth + gap + data.opt_tasks.length * cellWidth / 2, 13,
      'Optimisation problems', {class: 'chart-column-group', 'text-anchor': 'middle'}));
    for (const [i, column] of columns.entries()) {
      const x = cx(i) + cellWidth / 2;
      const label = mobile ? (column.short || column.label) : column.label;
      const lines = wrapLabel(label, cellWidth - 4);
      const node = el('text', {x, y: top - 7, class: 'chart-column-label', 'text-anchor': 'middle'});
      lines.forEach((line, index) => node.append(el('tspan', {x, dy: index === 0 ? -(lines.length - 1) * 12 : 12}, [line])));
      svg.append(node);
    }
    const rows = [];
    for (const [i, model] of models.entries()) {
      const rowTop = top + i * 20;
      const frame = modelRow(svg, model, {left, width, top: rowTop, pitch: 20, height: 18});
      const {row} = frame;
      const cells = [];
      for (const [j, column] of columns.entries()) {
        const score = column.category ? {hits: model.categories[column.id], total: data.suite.scenarios} : model.opt[column.id];
        const rate = score.total ? score.hits / score.total : 0;
        const cell = el('rect', {x: cx(j), y: rowTop, width: Math.max(1, cellWidth - 2), height: 18, rx: 1,
          fill: shade(rate), class: 'chart-heat-cell', 'data-column-id': column.id});
        const lines = [[column.label, `${int(score.hits)}/${int(score.total)} (${pct(rate, 1)})`]];
        const events = bindMark(cell, model, lines, store, tip,
          () => markPosition(svg, root, cx(j) + (cellWidth - 2) / 2, rowTop + 9));
        cell.addEventListener('pointermove', events.move);
        cell.addEventListener('pointerleave', events.leave);
        row.append(cell);
        cells.push({cell});
      }
      rows.push({frame, cells});
    }
    return state => {
      for (const {frame, cells} of rows) frame.sync(state, cells.map(({cell}) => cell));
    };
  });
  const key = document.createElement('div');
  key.className = 'chart-scale-key';
  key.setAttribute('aria-label', 'Pass-rate scale: zero is unfilled, then seven equal bins from above zero to 100 percent');
  const low = document.createElement('span');
  low.textContent = '0%';
  key.append(low);
  for (let i = 1; i <= 7; i++) {
    const swatch = document.createElement('span');
    swatch.className = 'chart-scale-swatch';
    swatch.style.background = `var(--seq-${i})`;
    swatch.setAttribute('aria-hidden', 'true');
    key.append(swatch);
  }
  const high = document.createElement('span');
  high.textContent = '100%';
  key.append(high);
  view.header.append(key);
  const caption = document.createElement('p');
  caption.className = 'chart-caption';
  caption.textContent = `${medians[0].label} has the lowest median pass rate (${pct(medians[0].rate, 1)}); ${medians.at(-1).label} has the highest (${pct(medians.at(-1).rate, 1)}).`;
  root.append(caption);
  return () => view.destroy();
}
