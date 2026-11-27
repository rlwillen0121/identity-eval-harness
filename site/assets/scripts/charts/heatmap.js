import { pct } from '../format.js';
import { bindMark, chart, el, markPosition, modelRow, nameColumn, svgCanvas, text, tipContent } from './svg.js';

export function heatmap(root, data, store, { title, subtitle, empty, columns, rates, scrollLabel = 'columns', columnWidth }) {
  const models = data.models.filter((model) => columns.some((col) => Number.isFinite(rates(model)?.[col.id])));
  const view = chart(root, title, subtitle, store, ({ width, canvas, tip }) => {
    if (!models.length) {
      const p = document.createElement('p');
      p.className = 'status-note muted';
      p.textContent = empty;
      canvas.append(p);
      return;
    }
    const minColumnWidth = columnWidth ?? (columns.length > 4 ? 51 : 72);
    const minimumWidth = columns.length * minColumnWidth + 154;
    const needsScroll = minimumWidth > width;
    const chartWidth = Math.max(width, minimumWidth);
    canvas.classList.toggle('heatmap-scroll', needsScroll);
    if (needsScroll) {
      const cue = document.createElement('p');
      cue.className = 'chart-scroll-cue';
      const cueId = `${root.getAttribute('id') || 'heatmap'}-scroll-cue`;
      cue.setAttribute('id', cueId);
      cue.setAttribute('role', 'note');
      cue.textContent = `Scroll horizontally to see all ${columns.length} ${scrollLabel}.`;
      canvas.append(cue);
      canvas.setAttribute('tabindex', '0');
      canvas.setAttribute('role', 'region');
      canvas.setAttribute('aria-label', `Scrollable heatmap by ${scrollLabel}`);
      canvas.setAttribute('aria-describedby', cueId);
    } else {
      canvas.removeAttribute('tabindex');
      canvas.removeAttribute('role');
      canvas.removeAttribute('aria-label');
      canvas.removeAttribute('aria-describedby');
    }
    const left = nameColumn(chartWidth, models);
    const rowH = 28;
    const top = columns.some((col) => col.lines?.length > 1) ? 43 : 34;
    const colW = (chartWidth - left - 16) / columns.length;
    const svg = svgCanvas(canvas, chartWidth, top + models.length * rowH + 12, title);
    columns.forEach((col, i) => {
      const x = left + (i + 0.5) * colW;
      const heading = text(x, 16, '', { 'text-anchor': 'middle', class: 'chart-column-label', 'aria-label': col.title });
      (col.lines ?? [col.short]).forEach((line, index) => {
        heading.append(el('tspan', { x, dy: index === 0 ? 0 : 13 }, [line]));
      });
      svg.append(heading);
    });

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
        const zero = valid && rate === 0;
        const bucket = valid ? Math.min(7, Math.max(1, Math.ceil(rate * 7))) : null;
        const x = left + ci * colW + 3;
        const w = colW - 6;
        const cell = el('rect', { x, y: y + 2, width: w, height: 18, rx: 3,
          fill: valid ? `var(--seq-${bucket})` : 'none', stroke: valid ? 'none' : 'var(--muted)',
          'stroke-dasharray': valid ? 'none' : '2 2', class: `chart-heat-cell${zero ? ' chart-heat-zero' : valid ? '' : ' chart-heat-unavailable'}`,
          'aria-hidden': 'true' });
        const label = text(x + w / 2, y + 14.5, valid ? pct(rate, 0) : '—',
          { 'text-anchor': 'middle', class: `chart-cell-label${valid && bucket >= 4 ? ' is-dark-cell' : ''}`, 'aria-hidden': 'true' });
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
    legend.className = 'chart-legend chart-heatmap-legend';
    legend.innerHTML = '<span><i class="heatmap-zero-key" aria-hidden="true"></i>0% observed</span><span><i class="heatmap-unavailable-key" aria-hidden="true"></i>Not reported</span><span class="chart-scale-key"><span>&gt;0%</span><span class="legend-ramp"><i></i><i></i><i></i><i></i><i></i><i></i><i></i></span><span>100%</span></span>';
    view.header.append(legend);
  }
  return () => view.destroy();
}
