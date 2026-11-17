import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import * as frontier from '../../../site/assets/scripts/charts/frontier.js';
import * as safety from '../../../site/assets/scripts/charts/safety.js';
import * as outcomes from '../../../site/assets/scripts/charts/outcomes.js';
import * as anatomy from '../../../site/assets/scripts/charts/anatomy.js';
import {installDOM, makeStore} from './dom-stub.mjs';

const data = JSON.parse(readFileSync(new URL('../../../site/data/leaderboard.json', import.meta.url), 'utf8'));
const charts = {frontier, safety, outcomes, anatomy};
const states = {
  rest: {}, hover: {hover: data.models[0].id}, selected: {selected: data.models[0].id},
  filtered: {vendors: new Set([data.models[0].vendor])}
};
const marks = root => root.querySelectorAll('[data-model-id]');
const visibleLabels = root => root.querySelectorAll('.chart-direct-label').filter(n => n.getAttribute('display') !== 'none');
const validAttributes = root => {
  for (const node of root.querySelectorAll('*')) for (const {name, value} of node.attributes) {
    assert.doesNotMatch(value, /NaN|undefined|Infinity/, `${node.tagName}.${name}=${value}`);
  }
};

for (const [name, chart] of Object.entries(charts)) for (const width of [328, 560, 1120]) {
  for (const [stateName, state] of Object.entries(states)) {
    test(`${name} mounts at ${width}px in ${stateName} state`, () => {
      const dom = installDOM();
      const root = dom.root(width);
      const store = makeStore(state);
      const destroy = chart.mount(root, data, store);
      validAttributes(root);
      const svgs = root.querySelectorAll('svg');
      assert.equal(svgs.length, 1);
      for (const svg of svgs) {
        assert.equal(svg.getAttribute('role'), 'group');
        assert.ok(svg.getAttribute('aria-label')?.trim());
      }
      const nodes = marks(root);
      assert.equal(new Set(nodes.map(n => n.getAttribute('data-model-id'))).size, data.models.length);
      assert.equal(nodes.length, data.models.length * (name === 'anatomy' ? data.categories.length + (width < 640 ? 0 : data.opt_tasks.length) : 1));
      for (const node of root.querySelectorAll('[tabindex]')) {
        assert.equal(node.getAttribute('role'), 'button');
        assert.equal(node.getAttribute('tabindex'), '0');
        assert.ok(node.getAttribute('aria-label')?.trim());
      }
      if (stateName === 'filtered') for (const label of visibleLabels(root)) {
        const model = data.models.find(m => m.id === label.getAttribute('data-label-model'));
        assert.ok(state.vendors.has(model.vendor));
      }
      // Store updates preserve the focusable DOM nodes.
      store.set({hover: data.models[1].id});
      assert.deepEqual(marks(root), nodes);
      validAttributes(root);
      destroy();
    });
  }
}

for (const [name, chart] of Object.entries(charts)) {
  test(`${name} supports keyboard tooltips, pinning, pointer tooltips and resizing`, () => {
    const dom = installDOM();
    const root = dom.root(560);
    const store = makeStore();
    const destroy = chart.mount(root, data, store);
    const node = marks(root)[0];
    const id = node.getAttribute('data-model-id');
    node.focus();
    assert.equal(store.get().hover, id);
    const tip = root.querySelector('.chart-tooltip');
    assert.equal(tip.hidden, false);
    assert.ok(tip.innerHTML.includes(data.models.find(m => m.id === id).name));
    const model = data.models.find(m => m.id === id);
    const ownLabelVisible = visibleLabels(root).some(n => n.getAttribute('data-label-model') === id);
    if (name === 'safety' && model.screen_clear) assert.ok(ownLabelVisible);
    else assert.ok(!ownLabelVisible);
    node.dispatchEvent({type: 'keydown', key: 'Enter'});
    assert.equal(store.get().selected, id);
    node.dispatchEvent({type: 'keydown', key: ' '});
    assert.equal(store.get().selected, id);
    node.dispatchEvent({type: 'keydown', key: 'Escape'});
    assert.equal(tip.hidden, true);
    node.blur();
    node.dispatchEvent({type: 'click'});
    assert.equal(store.get().selected, id);
    const target = name === 'anatomy' || name === 'outcomes' ? node : root.querySelector('.chart-hit-area');
    const circle = node.querySelector('circle');
    const x = circle ? Number(circle.getAttribute('cx')) : 200;
    const y = circle ? Number(circle.getAttribute('cy')) : 70;
    target.dispatchEvent({type: 'pointermove', clientX: x, clientY: y});
    assert.equal(tip.hidden, false);
    target.dispatchEvent({type: 'pointerleave'});
    assert.equal(tip.hidden, true);
    dom.resize(root, 328);
    validAttributes(root);
    assert.equal(root.querySelector('svg').getAttribute('width'), '328');
    destroy();
  });
}

test('outcomes preserves rank, four-count tooltips, positive segments and the computed caption', () => {
  const dom = installDOM();
  const root = dom.root(560);
  const destroy = outcomes.mount(root, data, makeStore());
  const rows = root.querySelectorAll('[data-row-model]');
  assert.deepEqual(rows.map(n => n.getAttribute('data-row-model')), data.models.slice().sort((a, b) => a.rank - b.rank).map(m => m.id));
  for (const row of rows) {
    const model = data.models.find(m => m.id === row.getAttribute('data-row-model'));
    const segments = row.querySelectorAll('[data-outcome]');
    assert.equal(segments.length, Object.values(model.outcomes).filter(n => n > 0).length);
    for (const segment of segments) assert.ok(Number(segment.getAttribute('width')) > 0);
    for (const [key, label] of [['perfect', 'All three waves'], ['two_waves', 'Two'], ['one_wave', 'One'], ['none', 'None']]) {
      assert.ok(row.getAttribute('aria-label').includes(`${label}: ${model.outcomes[key]}`));
    }
  }
  const count = data.models.filter(m => m.outcomes.perfect > data.suite.scenarios / 4).length;
  assert.match(root.querySelector('.chart-caption').textContent, new RegExp(`Only ${count} of ${data.models.length}`));
  destroy();
});

test('outcomes and anatomy show full desktop model names when the name column is not capped', () => {
  const dom = installDOM();
  const names = data.models.map((model, index) => ({...model, name: index ? model.name : 'A deliberately long model'}));
  const namedData = {...data, models: names};
  for (const chart of [outcomes, anatomy]) {
    for (const width of [560, 1120]) {
      const root = dom.root(width);
      const destroy = chart.mount(root, namedData, makeStore());
      for (const row of root.querySelectorAll('[data-row-model]')) {
        const model = names.find(item => item.id === row.getAttribute('data-row-model'));
        assert.equal(row.querySelector('.chart-row-label').textContent, model.name);
      }
      destroy();
    }
  }
});

test('anatomy category and optimisation labels preserve their source casing while wrapping', () => {
  const dom = installDOM();
  for (const width of [328, 1120]) {
    const root = dom.root(width);
    const destroy = anatomy.mount(root, data, makeStore());
    const expected = data.categories.map(item => width < 640 ? (item.short || item.label) : item.label)
      .concat(width < 640 ? [] : data.opt_tasks.map(item => item.label));
    const actual = root.querySelectorAll('.chart-column-label').map(node => node.children.map(line => line.textContent).join(' '));
    assert.deepEqual(actual, expected);
    destroy();
  }
});

const safetyClearIds = data.models.filter(model => model.screen_clear).map(model => model.id);
test('safety gives every screen-clear label priority at rest and while models are selected', () => {
  const dom = installDOM();
  for (const width of [328, 560, 1120]) {
    const root = dom.root(width);
    const store = makeStore();
    const destroy = safety.mount(root, data, store);
    const assertAllClearLabelsVisible = state => {
      store.set(state);
      const visible = new Set(visibleLabels(root).map(node => node.getAttribute('data-label-model')));
      for (const id of safetyClearIds) assert.ok(visible.has(id), `${width}px: screen-clear label ${id} was dropped`);
    };
    assertAllClearLabelsVisible({selected: null, hover: null});
    for (const model of data.models) assertAllClearLabelsVisible({selected: model.id, hover: null});
    destroy();
  }
});

test('safety screen-clear labels remain visible during their own tooltip and distant labels have one-pixel leaders', () => {
  const dom = installDOM();
  const css = readFileSync(new URL('../../../site/assets/styles/charts.css', import.meta.url), 'utf8');
  assert.match(css, /\.chart-label-leader\s*\{\s*stroke:\s*var\(--axis\);\s*stroke-width:\s*1px;/u);
  for (const width of [328, 560, 1120]) {
    const root = dom.root(width);
    const store = makeStore();
    const destroy = safety.mount(root, data, store);
    for (const id of safetyClearIds) {
      const mark = marks(root).find(node => node.getAttribute('data-model-id') === id);
      mark.focus();
      assert.ok(visibleLabels(root).some(node => node.getAttribute('data-label-model') === id), `${width}px: focused screen-clear label ${id} was hidden`);
      mark.blur();
    }
    const labels = visibleLabels(root);
    for (const label of labels) {
      const id = label.getAttribute('data-label-model');
      const model = data.models.find(item => item.id === id);
      const circle = marks(root).find(node => node.getAttribute('data-model-id') === id).querySelector('circle');
      const x = Number(circle.getAttribute('cx'));
      const y = Number(circle.getAttribute('cy'));
      const left = Number(label.getAttribute('x')) - 3;
      const top = Number(label.getAttribute('y')) - 12;
      const textWidth = Array.from(model.name).length * 7.2 + 6;
      const dx = Math.max(left - x, 0, x - left - textWidth);
      const dy = Math.max(top - y, 0, y - top - 16);
      if (Math.hypot(dx, dy) > 14) {
        const leader = root.querySelectorAll('.chart-label-leader').find(node => node.getAttribute('data-label-model') === id);
        assert.ok(leader, `${width}px: ${model.name} needs a leader`);
      }
    }
    destroy();
  }
});

test('safety plot follows the outcomes root height only at the side-by-side viewport breakpoint', () => {
  const dom = installDOM();
  dom.setViewportWidth(1120);
  const grid = document.createElement('div');
  const safetyRoot = dom.root(544, 500);
  const outcomesRoot = dom.root(544, 900);
  safetyRoot.setAttribute('id', 'chart-safety');
  outcomesRoot.setAttribute('id', 'chart-outcomes');
  grid.append(safetyRoot, outcomesRoot);
  const destroyOutcomes = outcomes.mount(outcomesRoot, data, makeStore());
  const destroySafety = safety.mount(safetyRoot, data, makeStore());
  const setSafetyChrome = () => {
    safetyRoot.querySelector('.chart-header').rect = {width: 544, height: 24};
    safetyRoot.querySelector('.chart-sub').rect = {width: 544, height: 40};
    safetyRoot.querySelector('.chart-legend').rect = {width: 544, height: 17};
  };
  setSafetyChrome();
  const expected = () => Math.max(354, outcomesRoot.rect.height - (24 + 40 + 8 + 16 + 17 + 12));
  dom.resize(outcomesRoot, 544, 900);
  assert.equal(Number(safetyRoot.querySelector('svg').getAttribute('height')), expected());
  dom.setViewportWidth(899);
  dom.resize(outcomesRoot, 544, 900);
  assert.equal(Number(safetyRoot.querySelector('svg').getAttribute('height')), 354);
  dom.setViewportWidth(900);
  dom.resize(outcomesRoot, 544, 900);
  assert.equal(Number(safetyRoot.querySelector('svg').getAttribute('height')), expected());
  dom.setViewportWidth(800);
  dom.resize(outcomesRoot, 800, 900);
  assert.equal(Number(safetyRoot.querySelector('svg').getAttribute('height')), 354);
  dom.setViewportWidth(1120);
  dom.resize(outcomesRoot, 544, 980);
  assert.equal(Number(safetyRoot.querySelector('svg').getAttribute('height')), expected());
  destroySafety();
  destroyOutcomes();
});

test('scatter peers keep their fill, frontier stays blue, scales stay fixed and default labels do not jump', () => {
  const dom = installDOM();
  const root = dom.root(1120);
  const store = makeStore();
  const destroy = frontier.mount(root, data, store);
  const nodes = marks(root);
  const fills = nodes.map(n => n.getAttribute('fill'));
  const positions = nodes.map(n => [n.querySelector('circle').getAttribute('cx'), n.querySelector('circle').getAttribute('cy')]);
  const labels = visibleLabels(root).map(n => [n, n.getAttribute('x'), n.getAttribute('y')]);
  const path = root.querySelector('.chart-frontier');
  const d = path.getAttribute('d');
  store.set({hover: data.models[0].id});
  nodes.forEach((node, i) => {
    if (node.getAttribute('data-model-id') !== data.models[0].id) assert.equal(node.getAttribute('fill'), fills[i]);
  });
  assert.equal(path.getAttribute('stroke'), 'var(--accent)');
  assert.equal(path.getAttribute('opacity'), '0.35');
  for (const [node, x, y] of labels) {
    assert.equal(node.getAttribute('x'), x);
    assert.equal(node.getAttribute('y'), y);
  }
  store.set({vendors: new Set([data.models[0].vendor]), hover: null});
  assert.equal(path.getAttribute('d'), d);
  assert.deepEqual(nodes.map(n => [n.querySelector('circle').getAttribute('cx'), n.querySelector('circle').getAttribute('cy')]), positions);
  store.set({query: 'no such model', selected: data.models[0].id});
  assert.equal(visibleLabels(root).length, 0);
  destroy();
});

test('chart interaction rectangles stay inside the SVG at narrow widths', () => {
  const dom = installDOM();
  for (const [name, chart] of Object.entries(charts)) {
    const root = dom.root(328);
    const destroy = chart.mount(root, data, makeStore());
    const svg = root.querySelector('svg');
    const height = Number(svg.getAttribute('height'));
    for (const rect of svg.querySelectorAll('rect')) {
      const x = Number(rect.getAttribute('x'));
      const y = Number(rect.getAttribute('y'));
      assert.ok(x >= 0 && x + Number(rect.getAttribute('width')) <= 328 + 1e-8, `${name}: horizontal rectangle bounds`);
      assert.ok(y >= 0 && y + Number(rect.getAttribute('height')) <= height + 1e-8, `${name}: vertical rectangle bounds`);
    }
    destroy();
  }
});

for (const [name, chart] of Object.entries({frontier})) {
  test(`${name} labels every pinned model and externally hovered model, suppressing only its own tooltip label`, () => {
    const dom = installDOM();
    const root = dom.root(328);
    const store = makeStore();
    const destroy = chart.mount(root, data, store);
    for (const model of data.models) {
      store.set({selected: model.id, hover: null});
      assert.ok(visibleLabels(root).some(n => n.getAttribute('data-label-model') === model.id), model.name);
      store.set({selected: null, hover: model.id});
      assert.ok(visibleLabels(root).some(n => n.getAttribute('data-label-model') === model.id), model.name);
      const node = marks(root).find(n => n.getAttribute('data-model-id') === model.id);
      node.focus();
      assert.ok(!visibleLabels(root).some(n => n.getAttribute('data-label-model') === model.id), model.name);
      node.blur();
    }
    destroy();
  });
}

test('safety allows lower-priority hover labels to drop while retaining every screen-clear label', () => {
  const dom = installDOM();
  const root = dom.root(328);
  const store = makeStore();
  const destroy = safety.mount(root, data, store);
  const nonClear = data.models.find(model => !model.screen_clear);
  store.set({selected: nonClear.id, hover: null});
  const visible = new Set(visibleLabels(root).map(node => node.getAttribute('data-label-model')));
  for (const id of safetyClearIds) assert.ok(visible.has(id), `screen-clear label ${id} lost priority`);
  destroy();
});
