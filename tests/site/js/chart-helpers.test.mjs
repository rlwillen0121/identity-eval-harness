import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {linear, log, power, ticks, placeLabels, markState, tooltip, nameColumn} from '../../../site/assets/js/charts/svg.js';
import {paretoFrontier} from '../../../site/assets/js/charts/frontier.js';
import {stackDots} from '../../../site/assets/js/charts/field.js';
import {shade} from '../../../site/assets/js/charts/anatomy.js';
import {installDOM} from './dom-stub.mjs';

const data = JSON.parse(readFileSync(new URL('../../../site/data/leaderboard.json', import.meta.url), 'utf8'));
const close = (a, b) => assert.ok(Math.abs(a - b) < 1e-9, `${a} ≠ ${b}`);

test('linear, log and power scales map endpoints and known interiors', () => {
  const scale = linear([2, 6], [10, 30]);
  assert.equal(scale(2), 10);
  assert.equal(scale(4), 20);
  assert.equal(scale(6), 30);
  assert.equal(linear([2, 2], [10, 30])(8), 20);
  close(log([1, 100], [0, 10])(10), 5);
  close(log([1, 100], [0, 10])(100), 10);
  close(power([0, 1], [100, 0], 0.5)(0.25), 50);
  close(power([0, 0.5], [300, 26], 0.4)(0.01), 300 + (0.01 ** 0.4 / 0.5 ** 0.4) * (26 - 300));
  assert.equal(power([0, 1], [300, 26])(0), 300);
  assert.equal(power([0, 1], [300, 26])(1), 26);
});

test('ticks include endpoints and handle a collapsed domain or zero count', () => {
  assert.deepEqual(ticks(), [0, 0.25, 0.5, 0.75, 1]);
  assert.deepEqual(ticks(10, 20, 2), [10, 15, 20]);
  assert.deepEqual(ticks(4, 4, 2), [4, 4, 4]);
  assert.deepEqual(ticks(0, 1, 0), [0]);
});

test('Pareto frontier excludes zeros and local cost, starts with the cheapest scoring model', () => {
  const model = (id, cost_usd, g_srr, cost_basis = 'retail_api') => ({id, cost_usd, g_srr, cost_basis});
  const models = [model('zero', 0.1, 0), model('local', 0.2, 1, 'local'), model('c', 10, 0.6),
    model('dominated', 5, 0.1), model('a', 1, 0.2), model('b', 2, 0.4), model('tie', 2, 0.3)];
  assert.deepEqual(paretoFrontier(models).map(m => m.id), ['a', 'b', 'c']);
  assert.deepEqual(paretoFrontier([]), []);
  const actual = paretoFrontier(data.models);
  assert.equal(actual[0].cost_usd, Math.min(...data.models.filter(m => m.cost_basis !== 'local' && m.cost_usd > 0 && m.g_srr > 0).map(m => m.cost_usd)));
  for (const [i, model] of actual.entries()) {
    assert.ok(model.g_srr > 0);
    assert.notEqual(model.cost_basis, 'local');
    if (i) assert.ok(model.g_srr > actual[i - 1].g_srr && model.cost_usd >= actual[i - 1].cost_usd);
  }
});

test('dot placement is deterministic, uses the lowest legal lane, and preserves exact x', () => {
  for (const width of [328, 560, 1120]) for (const spacing of [8, 9]) {
    const scale = linear([0, 1], [12, width - 22]);
    const points = stackDots(data.models, scale, spacing);
    assert.deepEqual(points, stackDots(data.models.slice().reverse(), scale, spacing));
    const zero = points.filter(p => p.model.g_srr === 0);
    assert.equal(Math.max(...zero.map(p => p.level)) + 1, zero.length);
    for (const [i, point] of points.entries()) {
      assert.equal(point.x, scale(point.model.g_srr));
      for (const other of points.slice(0, i)) {
        assert.ok(Math.hypot(point.x - other.x, (point.level - other.level) * spacing) >= spacing - 1e-9);
      }
      for (let lane = 0; lane < point.level; lane++) {
        assert.ok(points.slice(0, i).some(p => p.level === lane && Math.abs(p.x - point.x) < spacing));
      }
    }
  }
});

test('placeLabels stays in bounds, separates boxes and avoids obstacles', () => {
  const bounds = {left: 0, right: 180, top: 0, bottom: 100};
  const obstacle = {x: 80, y: 30, width: 25, height: 25};
  const input = [
    {x: 20, y: 30, label: 'Alpha'}, {x: 22, y: 31, label: 'Beta'},
    {x: 170, y: 80, label: 'Gamma'}, {x: 179, y: 0, label: 'Delta'},
    {x: 80, y: 40, label: 'Epsilon'}
  ];
  const placed = placeLabels(input, bounds, [obstacle]);
  assert.ok(placed.length >= 3);
  const overlaps = (a, b) => a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y;
  for (const [i, {box}] of placed.entries()) {
    assert.ok(box.x >= bounds.left && box.y >= bounds.top);
    assert.ok(box.x + box.width <= bounds.right && box.y + box.height <= bounds.bottom);
    for (const other of [obstacle, ...placed.slice(0, i).map(p => p.box)]) assert.ok(!overlaps(box, other));
  }
  assert.equal(placeLabels([{x: 0, y: 0, label: 'too wide', labelWidth: 200}], bounds).length, 0);
});

test('model name sizing measures the rendered font, falls back to 6.7px per character, and caps at 38%', () => {
  const names = [{name: 'Short'}, {name: 'Longest model name'}];
  const dom = installDOM();
  let measuredFont = '';
  dom.setCanvasMeasureText((value, font) => {
    measuredFont = font;
    return Array.from(value).length * 10;
  });
  assert.equal(nameColumn(1120, names), Array.from(names[1].name).length * 10 + 12);
  assert.match(measuredFont, /12px.*Test Sans/u);

  installDOM();
  assert.equal(nameColumn(1120, names), Array.from(names[1].name).length * 6.7 + 12);
  assert.equal(nameColumn(300, [{name: 'X'.repeat(100)}]), 300 * 0.38);
});

test('heatmap uses seven equal bins, with zero separate', () => {
  assert.equal(shade(0), 'var(--seq-0)');
  assert.equal(shade(Number.EPSILON), 'var(--seq-1)');
  assert.equal(shade(1), 'var(--seq-7)');
  for (let i = 1; i < 7; i++) {
    assert.equal(shade(i / 7), `var(--seq-${i})`);
    assert.equal(shade(i / 7 + 1e-8), `var(--seq-${i + 1})`);
    assert.equal(shade(i / 7 - 1e-8), `var(--seq-${i})`);
  }
});

test('hover changes opacity without repainting peers; selection does not dim peers', () => {
  const model = data.models[0];
  const state = {vendors: new Set(), query: '', hover: 'other', selected: null};
  const style = markState(model, state, 'var(--good)', 0.8);
  assert.equal(style.color, 'var(--good)');
  assert.equal(style.opacity, 0.3);
  assert.equal(markState(model, {...state, hover: null, selected: 'other'}).opacity, 1);
  assert.equal(markState(model, {...state, hover: null, selected: model.id}).color, 'var(--accent)');
});

test('tooltip prefers above/right, flips at edges and remains inside the root', () => {
  const dom = installDOM();
  const root = dom.root(400, 400);
  const tip = tooltip(root);
  const node = root.querySelector('.chart-tooltip');
  for (const [x, y, expectedX, expectedY] of [[100, 200, 112, 68], [350, 200, 158, 68], [100, 20, 112, 32], [350, 20, 158, 32]]) {
    tip.show('model', x, y, 'id');
    assert.equal(Number.parseFloat(node.style.left), expectedX);
    assert.equal(Number.parseFloat(node.style.top), expectedY);
    assert.ok(x < expectedX || x > expectedX + 180 || y < expectedY || y > expectedY + 120);
  }
  tip.hide();
  assert.ok(node.hidden);
  assert.equal(tip.modelId, null);
});
