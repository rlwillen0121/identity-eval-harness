import { endToEndPct, int, pct } from '../format.js';
import { bindMark, chart, el, linear, markPosition, modelRow, nameColumn, svgCanvas, text, ticks } from './svg.js';

export function mount(root, data, store) {
  const models = data.models.slice().sort((a, b) => a.rank - b.rank);
  const subtitle = 'Grounded resolution (G-SRR with 95% CI) compared against committed semantic resolution (Sem-SRR@commit).';
  const view = chart(root, 'Protocol grounding vs. semantic accuracy', subtitle, store, ({ width, canvas, tip }) => {
    const left = nameColumn(width, models);
    const right = width - 16;
    const top = 26;
    const pitch = 20;
    const height = top + models.length * pitch + 24;
    const x = linear([0, 1], [left, right]);
    const lead = models[0];
    const svg = svgCanvas(canvas, width, height,
      `${int(models.length)} protocol-complete v5 configurations. ${lead.name} leads G-SRR at ${endToEndPct(lead.g_srr)} (${pct(lead.sem_srr_commit)} Sem-SRR@commit).`);

    for (const value of ticks(0, 1, 4)) {
      const gx = x(value);
      svg.append(
        el('line', { x1: gx, x2: gx, y1: top - 4, y2: top + models.length * pitch - 4, class: 'chart-grid' }),
        text(gx, top - 9, pct(value, 0), { 'text-anchor': 'middle', class: 'chart-tick' }),
      );
    }

    const rows = models.map((model, i) => {
      const rowTop = top + i * pitch;
      const cy = rowTop + 9;
      const frame = modelRow(svg, model, { left, width, top: rowTop, pitch, height: 18 });
      const gx = x(model.g_srr);
      const sx = x(model.sem_srr_commit);
      const [ciLo, ciHi] = model.g_srr_ci.map((v) => x(v));

      const gapLine = el('line', {
        x1: Math.min(gx, sx),
        x2: Math.max(gx, sx),
        y1: cy,
        y2: cy,
        stroke: 'var(--axis)',
        'stroke-width': 2,
      });
      const ciLine = el('line', {
        x1: ciLo,
        x2: ciHi,
        y1: cy,
        y2: cy,
        stroke: 'var(--accent)',
        'stroke-width': 1.5,
      });
      const semDot = el('circle', {
        cx: sx,
        cy,
        r: 4,
        fill: 'var(--surface)',
        stroke: 'var(--ink-2)',
        'stroke-width': 1.8,
      });
      const gsrrDot = el('circle', {
        cx: gx,
        cy,
        r: 4.5,
        fill: 'var(--accent)',
      });
      frame.row.append(gapLine, ciLine, semDot, gsrrDot);
      frame.row.append(el('rect', { x: 0, y: rowTop - 1, width, height: pitch, fill: 'transparent' }));

      const lines = [
        ['G-SRR', `${endToEndPct(model.g_srr)} (${endToEndPct(model.g_srr_ci[0])}–${endToEndPct(model.g_srr_ci[1])})`],
        ['Sem-SRR@commit', pct(model.sem_srr_commit)],
        ['Grounding gap', pct(Math.max(0, model.sem_srr_commit - model.g_srr))],
      ];
      const events = bindMark(frame.row, model, lines, store, tip,
        () => markPosition(svg, root, (gx + sx) / 2, cy));
      frame.row.addEventListener('pointermove', events.move);
      frame.row.addEventListener('pointerleave', events.leave);
      return { frame, marks: [gapLine, ciLine, semDot, gsrrDot] };
    });
    return (state) => { for (const { frame, marks } of rows) frame.sync(state, marks); };
  });

  const legend = document.createElement('div');
  legend.className = 'chart-outcome-key';
  for (const [label, bg, border] of [
    ['G-SRR (with 95% CI)', 'var(--accent)', 'var(--accent)'],
    ['Sem-SRR@commit', 'var(--surface)', 'var(--ink-2)'],
  ]) {
    const item = document.createElement('span');
    const swatch = document.createElement('span');
    swatch.className = 'chart-scale-swatch';
    swatch.style.background = bg;
    swatch.style.border = `1.5px solid ${border}`;
    swatch.setAttribute('aria-hidden', 'true');
    item.append(swatch, document.createTextNode(label));
    legend.append(item);
  }
  view.header.append(legend);
  return () => view.destroy();
}
