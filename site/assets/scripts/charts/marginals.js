import { pct } from '../format.js';
import { chart, el, linear, modelRow, nameColumn, svgCanvas, text } from './svg.js';

const REGIME_SHORT = {
  R1: 'Zero-Trust',
  R2: 'Multi-Affil.',
  R3: 'Hybrid Cloud',
  R4: 'M&A',
};

function heatLegend() {
  const elLegend = document.createElement('div');
  elLegend.className = 'chart-legend';
  elLegend.setAttribute('aria-hidden', 'true');
  elLegend.innerHTML = '<span>0%</span><span class="legend-ramp"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></span><span>100%</span>';
  return elLegend;
}

export function mount(root, data, store) {
  const models = data.models || [];
  const regimes = data.regimes || [];
  const view = chart(root, 'By enterprise environment', 'End-to-end score across four operating models.', store, ({ width, canvas, tip }) => {
    if (!models.length) {
      const p = document.createElement('p');
      p.className = 'status-note muted';
      p.textContent = 'Awaiting completed runs to plot environment scores.';
      canvas.append(p);
      return () => {};
    }
    const left = nameColumn(width, models);
    const rowH = 28;
    const top = 34;
    const colW = (width - left - 16) / Math.max(1, regimes.length);
    const height = top + models.length * rowH + 22;
    const svg = svgCanvas(canvas, width, height, 'End-to-end resolution heatmap across four enterprise environments.');
    regimes.forEach((reg, ci) => {
      svg.append(text(left + ci * colW + colW / 2, 18, REGIME_SHORT[reg.id] || reg.id, { 'text-anchor': 'middle', class: 'chart-column-label' }));
    });
    const scale = linear([0, 1], [0, 7]);
    const rows = models.map((model, ri) => {
      const y = top + ri * rowH;
      const rowCtl = modelRow(svg, model, { left, width, top: y, pitch: rowH, height: 22 });
      const marks = [];
      const regMap = model.marginals?.regime_g_srr || {};
      regimes.forEach((reg, ci) => {
        const raw = regMap[reg.id];
        const valid = typeof raw === 'number' && Number.isFinite(raw);
        const rate = valid ? raw : 0;
        const bucket = valid && rate > 0 ? Math.max(1, Math.min(7, Math.ceil(scale(rate)))) : 0;
        const cellX = left + ci * colW + 4;
        const cellW = Math.max(10, colW - 8);
        const cell = el('rect', {
          x: cellX,
          y: y + 2,
          width: cellW,
          height: 18,
          rx: 3,
          fill: `var(--seq-${bucket})`,
          class: 'chart-heat-cell',
        });
        const label = text(cellX + cellW / 2, y + 14.5, valid ? pct(rate, 0) : 'n/a', {
          'text-anchor': 'middle',
          class: `chart-cell-label${bucket >= 4 ? ' is-dark-cell' : ''}`,
        });
        cell.addEventListener('pointerenter', () => {
          tip.show(`<strong>${model.name}</strong><div><span>${reg.label}</span><b>${valid ? pct(rate, 1) : 'n/a'}</b></div>`, cellX, y, model.id);
        });
        cell.addEventListener('pointerleave', () => tip.hide());
        svg.append(cell, label);
        marks.push(cell, label);
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
