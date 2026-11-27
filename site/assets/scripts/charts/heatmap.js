import { pct } from '../format.js';
import { bindMark, chart, el, markPosition, modelRow, nameColumn, svgCanvas, text, tipContent } from './svg.js';

export function heatmap(root, data, store, { title, subtitle, empty, columns, rates }) {
  const models = data.models.filter((model) => columns.some((col) => Number.isFinite(rates(model)?.[col.id])));
  const view = chart(root, title, subtitle, store, ({ width, canvas, tip }) => {
    if (!models.length) {
      const p = document.createElement('p');
      p.className = 'status-note muted';
      p.textContent = empty;
      canvas.append(p);
      return;
    }
    // Scroll the matrix on narrow screens so row labels and percentages stay readable.
    const chartWidth = Math.max(width, columns.length * (columns.length > 4 ? 51 : 72) + 154);
    canvas.classList.add('heatmap-scroll');
    const left = nameColumn(chartWidth, models);
    const rowH = 28;
    const top = 34;
    const colW = (chartWidth - left - 16) / columns.length;
    const svg = svgCanvas(canvas, chartWidth, top + models.length * rowH + 12, title);
    columns.forEach((col, i) => svg.append(text(left + (i + 0.5) * colW, 18, col.short,
      { 'text-anchor': 'middle', class: 'chart-column-label' })));

    const rows = models.map((model, ri) => {
      const y = top + ri * rowH;
      const rowCtl = modelRow(svg, model, { left, width: chartWidth, top: y, pitch: rowH, height: 22 });
      const lines = columns.map((col) => [col.title, pct(rates(model)?.[col.id], 1)]);
      const events = bindMark(rowCtl.row, model, lines, store, tip, () => markPosition(svg, root, left, y));
      rowCtl.row.addEventListener('pointerleave', events.leave);
      rowCtl.name.addEventListener('pointerenter', events.show);
      for (const [ci, col] of columns.entries()) {
        const rate = rates(model)?.[col.id];
        const valid = Number.isFinite(rate);
        const bucket = valid ? Math.min(7, Math.ceil(rate * 7)) : null;
        const x = left + ci * colW + 3;
        const w = colW - 6;
        const cell = el('rect', { x, y: y + 2, width: w, height: 18, rx: 3,
          fill: valid ? `var(--seq-${bucket})` : 'none', class: 'chart-heat-cell' });
        const label = text(x + w / 2, y + 14.5, valid ? pct(rate, 0) : '—',
          { 'text-anchor': 'middle', class: `chart-cell-label${valid && bucket >= 4 ? ' is-dark-cell' : ''}` });
        cell.addEventListener('pointerenter', () => {
          store.set({ hover: model.id });
          const [px, py] = markPosition(svg, root, x, y);
          tip.show(tipContent(model, [lines[ci]]), px, py, model.id);
        });
        rowCtl.row.append(cell, label);
      }
      return rowCtl;
    });
    return (state) => {
      for (const row of rows) {
        row.sync(state, []);
        for (const cell of row.row.querySelectorAll('.chart-heat-cell, .chart-cell-label')) {
          cell.setAttribute('opacity', row.name.getAttribute('opacity'));
        }
      }
    };
  });
  if (models.length) {
    const legend = document.createElement('div');
    legend.className = 'chart-legend';
    legend.setAttribute('aria-hidden', 'true');
    legend.innerHTML = '<span>0%</span><span class="legend-ramp"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></span><span>100%</span>';
    view.header.append(legend);
  }
  return () => view.destroy();
}
