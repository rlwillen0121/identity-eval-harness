import { pct } from '../format.js';
import { chart, el, linear, modelRow, nameColumn, svgCanvas, text } from './svg.js';

const REGIME_SHORT = {
  R1: 'Zero-Trust',
  R2: 'Multi-Affil.',
  R3: 'Hybrid Cloud',
  R4: 'M&A',
};

export function mount(root, data, store) {
  const models = data.models || [];
  const regimes = data.regimes || [];
  const view = chart(root, 'Performance by enterprise environment', 'End-to-end score across four enterprise operating models: Zero-Trust, Academic/Healthcare, Hybrid Cloud/ERP, and M&A.', store, ({ width, canvas, tip }) => {
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
        const rate = regMap[reg.id] ?? 0;
        const bucket = rate > 0 ? Math.max(1, Math.min(7, Math.ceil(scale(rate)))) : 0;
        const cell = el('rect', {
          x: left + ci * colW + 4,
          y: y + 2,
          width: Math.max(10, colW - 8),
          height: 18,
          rx: 3,
          fill: `var(--seq-${bucket})`,
          class: 'chart-heat-cell',
        });
        cell.addEventListener('pointerenter', () => {
          tip.show(`<strong>${model.name}</strong><div><span>${reg.label}</span><b>${pct(rate, 1)}</b></div>`, left + ci * colW, y, model.id);
        });
        cell.addEventListener('pointerleave', () => tip.hide());
        svg.append(cell);
        marks.push(cell);
      });
      return { rowCtl, marks };
    });
    return (state) => {
      for (const r of rows) r.rowCtl.sync(state, r.marks);
    };
  });
  return () => view.destroy();
}
