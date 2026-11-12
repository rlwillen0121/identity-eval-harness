import {endToEndPct, int} from '../format.js';
import {vendorLogoBody} from '../logos.js';
import {bindMark, chart, el, markPosition, markState, svgCanvas, text} from './svg.js';

export const TOP_COUNT = 10;

// The leading models in rank order, independent of the order of the data file.
export function topModels(models, limit = TOP_COUNT) {
  return models.slice().sort((a, b) => a.rank - b.rank || a.id.localeCompare(b.id)).slice(0, Math.max(0, limit));
}

function fitName(name, width) {
  const perChar = 7.1;
  const chars = Array.from(String(name));
  if (chars.length * perChar <= width) return String(name);
  const keep = Math.max(1, Math.floor(width / perChar) - 1);
  return `${chars.slice(0, keep).join('').trimEnd()}…`;
}

const VALUE_COLUMN = 58;
const LOGO = 15;

export function mount(root, data, store) {
  root.classList.add('chart-field');
  const top = topModels(data.models);
  const zeroCount = data.models.filter(model => !(model.g_srr > 0)).length;
  const view = chart(root, null, null, store, ({width, canvas, tip}) => {
    const small = width < 420;
    const barHeight = small ? 30 : 32;
    const pitch = barHeight + 6;
    const trackWidth = Math.max(1, width - VALUE_COLUMN);
    const height = Math.max(1, top.length * pitch - 6);
    const leader = top[0];
    const svg = svgCanvas(canvas, width, height, leader
      ? `The ${int(top.length)} highest-ranked models by share of scenarios resolved end to end. ${leader.name} leads at ${endToEndPct(leader.g_srr)}.`
      : 'No models to show.');
    const rows = top.map((model, i) => {
      const y = i * pitch;
      const mid = y + barHeight / 2;
      const row = el('g', {class: 'chart-top-row', 'data-row-model': model.id});
      const fillWidth = Math.max(barHeight, Math.min(1, Math.max(0, model.g_srr)) * trackWidth);
      const track = el('rect', {x: 0, y, width: trackWidth, height: barHeight, rx: 6, class: 'chart-top-track'});
      const bar = el('rect', {x: 0, y, width: fillWidth, height: barHeight, rx: 6, class: 'chart-top-bar',
        'data-rate': model.g_srr});
      const rank = text(11, mid + 4, int(model.rank), {class: 'chart-top-rank'});
      const logo = el('g', {class: 'chart-top-logo', 'aria-hidden': 'true',
        transform: `translate(34 ${mid - LOGO / 2}) scale(${LOGO / 24})`});
      logo.innerHTML = vendorLogoBody(model.vendor, model.vendor_label);
      const name = text(58, mid + 4.5, fitName(model.name, trackWidth - 66), {class: 'chart-top-name'});
      const value = text(width - 2, mid + 4.5, endToEndPct(model.g_srr), {class: 'chart-top-value', 'text-anchor': 'end'});
      const outline = el('rect', {x: 0.75, y: y + 0.75, width: trackWidth - 1.5, height: barHeight - 1.5, rx: 5.5,
        fill: 'none', stroke: 'none', 'stroke-width': 1.5, 'pointer-events': 'none'});
      const hitTop = Math.max(0, y - 3);
      const hit = el('rect', {x: 0, y: hitTop, width, height: Math.min(pitch, height - hitTop), fill: 'transparent'});
      row.append(track, bar, rank, logo, name, value, outline, hit);
      svg.append(row);
      const events = bindMark(row, model, [
        ['End to end', endToEndPct(model.g_srr)],
        ['Scenarios resolved', `${int(model.outcomes?.perfect)} of ${int(data.suite.scenarios)}`],
        ['Rank', `#${int(model.rank)}`],
      ], store, tip, () => markPosition(svg, root, fillWidth, mid));
      row.addEventListener('pointermove', events.move);
      row.addEventListener('pointerleave', events.leave);
      return {model, row, name, outline};
    });
    return state => {
      for (const {model, row, name, outline} of rows) {
        const style = markState(model, state);
        const emphasised = style.active && !style.filtered;
        row.setAttribute('opacity', style.opacity);
        row.setAttribute('aria-pressed', state.selected === model.id ? 'true' : 'false');
        name.style.fontWeight = emphasised ? '600' : '500';
        outline.setAttribute('stroke', emphasised ? 'var(--accent)' : 'none');
      }
    };
  });

  const heading = document.createElement('div');
  heading.className = 'chart-top-header';
  const title = document.createElement('h2');
  title.className = 'chart-top-title';
  title.textContent = `Top ${int(top.length)}`;
  const chip = document.createElement('span');
  chip.className = 'chart-chip';
  chip.textContent = 'End to end';
  const scope = document.createElement('span');
  scope.className = 'chart-top-scope';
  scope.textContent = `of ${int(data.suite.scenarios)} scenarios`;
  heading.append(title, chip, scope);

  const footer = document.createElement('p');
  footer.className = 'chart-top-note';
  footer.textContent = zeroCount
    ? `${int(zeroCount)} of ${int(data.models.length)} models resolve none.`
    : `${int(data.models.length)} models evaluated.`;
  root.replaceChildren(heading, ...Array.from(root.childNodes), footer);
  return () => view.destroy();
}
