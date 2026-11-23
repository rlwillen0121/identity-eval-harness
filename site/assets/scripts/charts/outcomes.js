import { int } from '../format.js';
import { OUTCOME_SEGMENTS } from '../detail.js';
import { bindMark, chart, el, markPosition, modelRow, nameColumn, svgCanvas } from './svg.js';

export { OUTCOME_SEGMENTS };

export function mount(root, data, store) {
  const models = data.models.slice().sort((a, b) => a.rank - b.rank);
  const slots = Number(data.suite?.slots_per_run);
  const subtitle = `Each model's ${int(slots)} episodes, by outcome.`;
  const view = chart(root, 'How far do models get?', subtitle, store, ({ width, canvas, tip }) => {
    const left = nameColumn(width, models);
    const top = 6;
    const pitch = 19;
    const height = top + models.length * pitch + 4;
    const barWidth = width - left - 4;
    const svg = svgCanvas(canvas, width, height,
      `${int(models.length)} models in rank order. Each bar divides ${int(slots)} episodes into ${OUTCOME_SEGMENTS.map(([, label]) => label).join(', ')}.`);
    const rows = models.map((model, i) => {
      const rowTop = top + i * pitch;
      const frame = modelRow(svg, model, { left, width, top: rowTop, pitch, height: 9 });
      const visible = OUTCOME_SEGMENTS.filter(([id]) => (model.outcomes?.[id] || 0) > 0);
      const available = barWidth - Math.max(0, visible.length - 1) * 1.5;
      let x = left;
      const bars = visible.map(([id, , color]) => {
        const segmentWidth = ((model.outcomes?.[id] || 0) / slots) * available;
        const bar = el('rect', {
          x,
          y: rowTop,
          width: segmentWidth,
          height: 9,
          fill: color,
          'data-outcome': id,
        });
        frame.row.append(bar);
        x += segmentWidth + 1.5;
        return bar;
      });
      // The whole row remains easy to point at even when a segment is very small.
      frame.row.append(el('rect', { x: 0, y: rowTop - 3, width, height: pitch, fill: 'transparent' }));
      const lines = OUTCOME_SEGMENTS.map(([id, label]) => [label, int(model.outcomes?.[id] || 0)]);
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
  for (const [, label, color] of OUTCOME_SEGMENTS) {
    const item = document.createElement('span');
    const swatch = document.createElement('span');
    swatch.className = 'chart-scale-swatch';
    swatch.style.background = color;
    swatch.setAttribute('aria-hidden', 'true');
    item.append(swatch, document.createTextNode(label));
    legend.append(item);
  }
  view.header.append(legend);
  const caption = document.createElement('p');
  caption.className = 'chart-caption';
  const count = models.filter((m) => (m.outcomes?.end_to_end || 0) > slots / 2).length;
  caption.textContent = `${count === models.length ? 'All' : `Only ${int(count)} of`} ${int(models.length)} models resolve more than half of the ${int(slots)} episodes end to end.`;
  root.append(caption);
  return () => view.destroy();
}
