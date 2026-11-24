import { pct } from '../format.js';
import { chart, el, linear, modelRow, nameColumn, svgCanvas, text } from './svg.js';

export function mount(root, data, store) {
  const models = data.models || [];
  const categories = data.categories || [];
  const view = chart(root, 'Pass rates across the 8 IGA categories', 'Category exact-pass rates per ranked entry (including NHI & AI-agent governance).', store, ({ width, canvas, tip }) => {
    if (!models.length) {
      const p = document.createElement('p');
      p.className = 'status-note muted';
      p.textContent = 'Awaiting ranked v5.2 entries to compare category pass rates.';
      canvas.append(p);
      return () => {};
    }
    const left = nameColumn(width, models);
    const rowH = 28;
    const top = 34;
    const colW = (width - left - 16) / Math.max(1, categories.length);
    const height = top + models.length * rowH + 22;
    const svg = svgCanvas(canvas, width, height, 'Heatmap of 8 IGA category pass rates by ranked model.');
    categories.forEach((cat, ci) => {
      svg.append(text(left + ci * colW + colW / 2, 18, cat.short, { 'text-anchor': 'middle', class: 'chart-column-label' }));
    });
    const xScale = linear([0, 1], [0, 7]);
    const rows = models.map((model, ri) => {
      const y = top + ri * rowH;
      const rowCtl = modelRow(svg, model, { left, width, top: y, pitch: rowH, height: 22 });
      const marks = [];
      categories.forEach((cat, ci) => {
        const rate = model.scores?.category_pass_rates?.[cat.id] ?? 0;
        const bucket = rate > 0 ? Math.max(1, Math.min(7, Math.ceil(xScale(rate)))) : 0;
        const cell = el('rect', {
          x: left + ci * colW + 3,
          y: y + 2,
          width: Math.max(8, colW - 6),
          height: 18,
          rx: 3,
          fill: `var(--seq-${bucket})`,
          class: 'chart-heat-cell',
        });
        cell.addEventListener('pointerenter', () => {
          tip.show(`<strong>${model.name}</strong><div><span>${cat.title}</span><b>${pct(rate, 1)}</b></div>`, left + ci * colW, y, model.id);
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
