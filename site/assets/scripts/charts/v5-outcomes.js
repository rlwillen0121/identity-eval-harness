import { int } from '../format.js';
import { bindMark, chart, el, markPosition, modelRow, nameColumn, svgCanvas } from './svg.js';

const segments = [
  ['g_srr_pass', 'G-SRR pass', 'var(--seq-6)'],
  ['completed_non_pass', 'Completed (non-pass)', 'var(--seq-3)'],
  ['turn_limits', 'Turn limit', 'var(--seq-2)'],
  ['output_limits', 'Output budget', 'var(--seq-1)'],
  ['parse_failures', 'Parse failure', 'var(--critical)'],
  ['context_limits', 'Context limit', 'var(--muted)'],
];

export function mount(root, data, store) {
  const models = data.models.slice().sort((a, b) => a.rank - b.rank);
  const totalSlots = data.suite.slots_per_run;
  const subtitle = `All ${int(totalSlots)} planned slots per complete configuration, with parse failures and limit stops shown separately.`;
  const view = chart(root, '144-slot outcomes & terminations', subtitle, store, ({ width, canvas, tip }) => {
    const left = nameColumn(width, models);
    const top = 6;
    const pitch = 19;
    const height = top + models.length * pitch + 4;
    const barWidth = width - left - 4;
    const svg = svgCanvas(canvas, width, height,
      `${int(models.length)} protocol-complete configurations in rank order dividing ${int(totalSlots)} slots into G-SRR pass, completed non-pass, turn limits, output budget stops, parse failures, and context limits.`);
    const rows = models.map((model, i) => {
      const rowTop = top + i * pitch;
      const frame = modelRow(svg, model, { left, width, top: rowTop, pitch, height: 9 });
      const visible = segments.filter(([id]) => (model.outcomes?.[id] || 0) > 0);
      const available = barWidth - Math.max(0, visible.length - 1) * 2;
      let x = left;
      const bars = visible.map(([id, , fill]) => {
        const segmentWidth = (model.outcomes[id] / totalSlots) * available;
        const bar = el('rect', { x, y: rowTop, width: segmentWidth, height: 9, fill, 'data-outcome': id });
        frame.row.append(bar);
        x += segmentWidth + 2;
        return bar;
      });
      frame.row.append(el('rect', { x: 0, y: rowTop - 3, width, height: pitch, fill: 'transparent' }));
      const lines = segments.map(([id, label]) => [label, int(model.outcomes?.[id] || 0)]);
      const events = bindMark(frame.row, model, lines, store, tip,
        () => markPosition(svg, root, left + barWidth / 2, rowTop + 4));
      frame.row.addEventListener('pointermove', events.move);
      frame.row.addEventListener('pointerleave', events.leave);
      return { frame, bars };
    });
    return (state) => { for (const { frame, bars } of rows) frame.sync(state, bars); };
  });
  const legend = document.createElement('div');
  legend.className = 'chart-outcome-key';
  for (const [, label, fill] of segments) {
    const item = document.createElement('span');
    const swatch = document.createElement('span');
    swatch.className = 'chart-scale-swatch';
    swatch.style.background = fill;
    swatch.setAttribute('aria-hidden', 'true');
    item.append(swatch, document.createTextNode(label));
    legend.append(item);
  }
  view.header.append(legend);
  return () => view.destroy();
}
