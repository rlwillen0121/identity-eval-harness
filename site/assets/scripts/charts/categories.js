import { pct } from '../format.js';
import { chart, el, linear, modelRow, nameColumn, svgCanvas, text } from './svg.js';

function heatLegend() {
  const elLegend = document.createElement('div');
  elLegend.className = 'chart-legend';
  elLegend.setAttribute('aria-hidden', 'true');
  elLegend.innerHTML = '<span>0%</span><span class="legend-ramp"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></span><span>100%</span>';
  return elLegend;
}

export function mount(root, data, store) {
  const models = data.models || [];
  const categories = data.categories || [];
  const view = chart(root, 'By IAM domain', 'Exact pass rate in each of the eight governance domains.', store, ({ width, canvas, tip }) => {
    if (!models.length) {
      const p = document.createElement('p');
      p.className = 'status-note muted';
      p.textContent = 'Awaiting completed runs to compare domain pass rates.';
      canvas.append(p);
      return () => {};
    }
    const left = nameColumn(width, models);
    const rowH = 28;
    const top = 34;
    const colW = (width - left - 16) / Math.max(1, categories.length);
    const height = top + models.length * rowH + 22;
    const svg = svgCanvas(canvas, width, height, 'Heatmap of pass rates across all 8 enterprise IAM domains.');
    categories.forEach((cat, ci) => {
      svg.append(text(left + ci * colW + colW / 2, 18, cat.short, { 'text-anchor': 'middle', class: 'chart-column-label' }));
    });
    const xScale = linear([0, 1], [0, 7]);
    const showCellText = colW >= 40;
    const rows = models.map((model, ri) => {
      const y = top + ri * rowH;
      const rowCtl = modelRow(svg, model, { left, width, top: y, pitch: rowH, height: 22 });
      const marks = [];
      categories.forEach((cat, ci) => {
        const raw = model.scores?.category_pass_rates?.[cat.id];
        const valid = typeof raw === 'number' && Number.isFinite(raw);
        const rate = valid ? raw : 0;
        const bucket = valid && rate > 0 ? Math.max(1, Math.min(7, Math.ceil(xScale(rate)))) : 0;
        const cellX = left + ci * colW + 3;
        const cellW = Math.max(8, colW - 6);
        const cell = el('rect', {
          x: cellX,
          y: y + 2,
          width: cellW,
          height: 18,
          rx: 3,
          fill: `var(--seq-${bucket})`,
          class: 'chart-heat-cell',
        });
        cell.addEventListener('pointerenter', () => {
          tip.show(`<strong>${model.name}</strong><div><span>${cat.title}</span><b>${valid ? pct(rate, 1) : 'n/a'}</b></div>`, cellX, y, model.id);
        });
        cell.addEventListener('pointerleave', () => tip.hide());
        svg.append(cell);
        marks.push(cell);
        if (showCellText) {
          const label = text(cellX + cellW / 2, y + 14.5, valid ? pct(rate, 0) : 'n/a', {
            'text-anchor': 'middle',
            class: `chart-cell-label${bucket >= 4 ? ' is-dark-cell' : ''}`,
          });
          svg.append(label);
          marks.push(label);
        }
      });
      return { rowCtl, marks };
    });
    return (state) => {
      for (const r of rows) r.rowCtl.sync(state, r.marks);
    };
  });
  view.header.append(heatLegend());
  return () => view.destroy();
}
