import {endToEndPct, int} from '../format.js';
import {vendorLogoBody} from '../logos.js';
import {bindMark, chart, el, linear, markPosition, markState, modelRow, nameColumn, svgCanvas, text} from './svg.js';

// The field in two groups, each in rank order: models that resolved at least one scenario end
// to end, and models that resolved none. The second group shares one score, so it is drawn as
// a row of vendor marks instead of a stack of identical bars.
export function splitField(models) {
  const ranked = models.slice().sort((a, b) => a.rank - b.rank || a.id.localeCompare(b.id));
  return {scoring: ranked.filter(model => model.g_srr > 0), zero: ranked.filter(model => !(model.g_srr > 0))};
}

// Row-major cells for a wrapped grid: the first row fills left to right, then the next.
export function gridCells(count, columns) {
  const perRow = Math.max(1, Math.floor(columns));
  return Array.from({length: Math.max(0, count)}, (_, i) => ({column: i % perRow, row: Math.floor(i / perRow)}));
}

const VALUE_COLUMN = 52;
const ZERO_MARK = 14;
const ZERO_CELL = 22;

export function mount(root, data, store) {
  root.classList.add('chart-field');
  const {scoring, zero} = splitField(data.models);
  const leader = scoring[0] || zero[0];
  const view = chart(root, null, null, store, ({width, canvas, tip}) => {
    const small = width < 480;
    const pitch = small ? 22 : 24;
    const barHeight = 6;
    const top = 4;
    const left = nameColumn(width, scoring);
    const trackRight = width - VALUE_COLUMN;
    const x = linear([0, 1], [left, trackRight]);
    const rowsBottom = top + scoring.length * pitch;
    const zeroColumns = Math.floor((width - 4) / ZERO_CELL);
    const cells = gridCells(zero.length, zeroColumns);
    const zeroRows = cells.length ? cells.at(-1).row + 1 : 0;
    const zeroTop = rowsBottom + 12;
    const height = zero.length ? zeroTop + 38 + zeroRows * ZERO_CELL + 2 : rowsBottom + 4;
    const summary = [
      `${int(scoring.length)} of ${int(data.models.length)} models resolved at least one scenario end to end; they are listed in rank order with a bar for the share resolved.`,
      leader ? `${leader.name} leads at ${endToEndPct(leader.g_srr)}.` : '',
      zero.length ? `${int(zero.length)} models resolved none.` : '',
    ].filter(Boolean).join(' ');
    const svg = svgCanvas(canvas, width, height, summary);

    const lines = model => [
      ['End to end', endToEndPct(model.g_srr)],
      ['Scenarios resolved', `${int(model.outcomes?.perfect)} of ${int(data.suite.scenarios)}`],
      ['Rank', `#${int(model.rank)}`],
    ];

    const rows = scoring.map((model, i) => {
      const rowTop = top + i * pitch + (pitch - barHeight) / 2;
      const frame = modelRow(svg, model, {left, width, top: rowTop, pitch, height: barHeight});
      const track = el('rect', {x: left, y: rowTop, width: Math.max(0, trackRight - left), height: barHeight,
        rx: barHeight / 2, class: 'chart-field-track'});
      const barWidth = Math.max(barHeight, x(model.g_srr) - left);
      const bar = el('rect', {x: left, y: rowTop, width: barWidth, height: barHeight, rx: barHeight / 2,
        class: 'chart-field-bar', 'data-rate': model.g_srr});
      const value = text(width - 2, rowTop + barHeight / 2 + 4, endToEndPct(model.g_srr),
        {class: 'chart-field-value', 'text-anchor': 'end'});
      // The whole row is the target, so short bars are as easy to point at as long ones.
      const hit = el('rect', {x: 0, y: top + i * pitch, width, height: pitch, fill: 'transparent'});
      frame.row.append(track, bar, value, hit);
      const events = bindMark(frame.row, model, lines(model), store, tip,
        () => markPosition(svg, root, left + barWidth, rowTop + barHeight / 2));
      frame.row.addEventListener('pointermove', events.move);
      frame.row.addEventListener('pointerleave', events.leave);
      return {frame, marks: [bar, value]};
    });

    const zeroMarks = [];
    if (zero.length) {
      svg.append(el('line', {x1: 0, x2: width, y1: zeroTop, y2: zeroTop, class: 'chart-field-rule'}),
        text(0, zeroTop + 22, `${int(zero.length)} models resolved none`, {class: 'chart-field-count'}));
      const gridTop = zeroTop + 34;
      for (const [i, model] of zero.entries()) {
        const cx = cells[i].column * ZERO_CELL + ZERO_CELL / 2;
        const cy = gridTop + cells[i].row * ZERO_CELL + ZERO_CELL / 2;
        const node = el('g', {class: 'chart-field-zero'});
        const mark = el('g', {transform: `translate(${cx - ZERO_MARK / 2} ${cy - ZERO_MARK / 2}) scale(${ZERO_MARK / 24})`,
          'aria-hidden': 'true'});
        mark.innerHTML = vendorLogoBody(model.vendor, model.vendor_label);
        node.append(mark, el('rect', {x: cx - ZERO_CELL / 2, y: cy - ZERO_CELL / 2, width: ZERO_CELL,
          height: ZERO_CELL, fill: 'transparent'}));
        const events = bindMark(node, model, lines(model), store, tip, () => markPosition(svg, root, cx, cy));
        node.addEventListener('pointermove', events.move);
        node.addEventListener('pointerleave', events.leave);
        svg.append(node);
        zeroMarks.push({node, model});
      }
    }

    return state => {
      for (const {frame, marks} of rows) frame.sync(state, marks);
      for (const {node, model} of zeroMarks) {
        const style = markState(model, state, 'var(--ink)');
        node.setAttribute('fill', style.color);
        node.setAttribute('opacity', style.opacity);
        node.setAttribute('aria-pressed', state.selected === model.id ? 'true' : 'false');
      }
    };
  });
  const heading = document.createElement('p');
  heading.className = 'chart-field-title';
  const label = document.createElement('span');
  label.textContent = 'Scenarios resolved end to end';
  const scope = document.createElement('span');
  scope.className = 'chart-field-scope';
  scope.textContent = `share of ${int(data.suite.scenarios)}`;
  heading.append(label, scope);
  root.replaceChildren(heading, ...Array.from(root.childNodes));
  return () => view.destroy();
}
