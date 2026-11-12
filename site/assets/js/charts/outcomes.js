import {int} from '../format.js';
import {bindMark, chart, el, markPosition, modelRow, nameColumn, svgCanvas} from './svg.js';

const segments = [
  ['perfect', 'All three waves', 6], ['two_waves', 'Two', 4],
  ['one_wave', 'One', 2], ['none', 'None', 0]
];

export function mount(root, data, store) {
  const models = data.models.slice().sort((a, b) => a.rank - b.rank);
  const subtitle = "Every scenario unfolds in three waves that build on each other. Each bar splits a model's 180 scenarios by how many waves it solved.";
  const view = chart(root, 'How far do models get?', subtitle, store, ({width, canvas, tip}) => {
    const left = nameColumn(width, models);
    const top = 6;
    const pitch = 14;
    const height = top + models.length * pitch + 4;
    const barWidth = width - left - 4;
    const svg = svgCanvas(canvas, width, height,
      `${int(models.length)} models in rank order. Each bar divides ${int(data.suite.scenarios)} scenarios into all three waves, two waves, one wave, or none solved.`);
    const rows = models.map((model, i) => {
      const rowTop = top + i * pitch;
      const frame = modelRow(svg, model, {left, width, top: rowTop, pitch, height: 8});
      const visible = segments.filter(([id]) => model.outcomes[id] > 0);
      const available = barWidth - Math.max(0, visible.length - 1) * 2;
      let x = left;
      const bars = visible.map(([id, , step]) => {
        const segmentWidth = model.outcomes[id] / data.suite.scenarios * available;
        const bar = el('rect', {x, y: rowTop, width: segmentWidth, height: 8,
          fill: `var(--seq-${step})`, 'data-outcome': id});
        frame.row.append(bar);
        x += segmentWidth + 2;
        return bar;
      });
      // The whole row remains easy to point at even when a segment is very small.
      frame.row.append(el('rect', {x: 0, y: rowTop - 3, width, height: pitch, fill: 'transparent'}));
      const lines = segments.map(([id, label]) => [label, int(model.outcomes[id])]);
      const events = bindMark(frame.row, model, lines, store, tip,
        () => markPosition(svg, root, left + barWidth / 2, rowTop + 4));
      frame.row.addEventListener('pointermove', events.move);
      frame.row.addEventListener('pointerleave', events.leave);
      return {frame, bars};
    });
    return state => { for (const {frame, bars} of rows) frame.sync(state, bars); };
  });
  const legend = document.createElement('div');
  legend.className = 'chart-outcome-key';
  for (const [, label, step] of segments) {
    const item = document.createElement('span');
    const swatch = document.createElement('span');
    swatch.className = 'chart-scale-swatch';
    swatch.style.background = `var(--seq-${step})`;
    swatch.setAttribute('aria-hidden', 'true');
    item.append(swatch, document.createTextNode(label));
    legend.append(item);
  }
  view.header.append(legend);
  const caption = document.createElement('p');
  caption.className = 'chart-caption';
  const count = models.filter(m => m.outcomes.perfect > data.suite.scenarios / 4).length;
  caption.textContent = `Only ${int(count)} of ${int(models.length)} models solve all three waves in more than a quarter of scenarios.`;
  root.append(caption);
  return () => view.destroy();
}
