import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import * as frontier from '../assets/scripts/charts/frontier.js';
import * as safety from '../assets/scripts/charts/safety.js';
import * as outcomes from '../assets/scripts/charts/outcomes.js';
import * as anatomy from '../assets/scripts/charts/anatomy.js';
import { installDOM, makeStore } from './dom-stub.mjs';

const data = JSON.parse(readFileSync(new URL('../data/leaderboard.json', import.meta.url), 'utf8'));
const charts = { frontier, safety, outcomes, anatomy };
const states = {
  rest: {},
  hover: { hover: data.models[0].id },
  selected: { selected: data.models[0].id },
  filtered: { vendors: new Set([data.models[0].vendor]) },
};
const marks = (root) => root.querySelectorAll('[data-model-id]');
const visibleLabels = (root) => root.querySelectorAll('.chart-direct-label').filter((n) => n.getAttribute('display') !== 'none');
const validAttributes = (root) => {
  for (const node of root.querySelectorAll('*')) {
    for (const { name, value } of node.attributes) {
      assert.doesNotMatch(value, /NaN|undefined|Infinity/, `${node.tagName}.${name}=${value}`);
    }
  }
};

for (const [name, chart] of Object.entries(charts)) {
  for (const width of [328, 560, 1120]) {
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
        assert.equal(new Set(nodes.map((n) => n.getAttribute('data-model-id'))).size, data.models.length);
        assert.equal(
          nodes.length,
          data.models.length * (name === 'anatomy' ? data.categories.length + (width < 640 ? 0 : data.families.length) : 1),
        );
        for (const node of root.querySelectorAll('[tabindex]')) {
          assert.equal(node.getAttribute('role'), 'button');
          assert.equal(node.getAttribute('tabindex'), '0');
          assert.ok(node.getAttribute('aria-label')?.trim());
        }
        if (stateName === 'filtered') {
          for (const label of visibleLabels(root)) {
            const model = data.models.find((m) => m.id === label.getAttribute('data-label-model'));
            assert.ok(state.vendors.has(model.vendor));
          }
        }
        store.set({ hover: data.models[1].id });
        assert.deepEqual(marks(root), nodes);
        validAttributes(root);
        destroy();
      });
    }
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
    assert.ok(tip.innerHTML.includes(data.models.find((m) => m.id === id).name));
    node.dispatchEvent({ type: 'keydown', key: 'Enter' });
    assert.equal(store.get().selected, id);
    node.dispatchEvent({ type: 'keydown', key: ' ' });
    assert.equal(store.get().selected, id);
    node.dispatchEvent({ type: 'keydown', key: 'Escape' });
    assert.equal(tip.hidden, true);
    node.blur();
    node.dispatchEvent({ type: 'click' });
    assert.equal(store.get().selected, id);
    const target = name === 'anatomy' || name === 'outcomes' ? node : root.querySelector('.chart-hit-area');
    const circle = node.querySelector('circle');
    const x = circle ? Number(circle.getAttribute('cx')) : 200;
    const y = circle ? Number(circle.getAttribute('cy')) : 70;
    target.dispatchEvent({ type: 'pointermove', clientX: x, clientY: y });
    assert.equal(tip.hidden, false);
    target.dispatchEvent({ type: 'pointerleave' });
    assert.equal(tip.hidden, true);
    dom.resize(root, 328);
    validAttributes(root);
    assert.equal(root.querySelector('svg').getAttribute('width'), '328');
    destroy();
  });
}

test('frontier chart-toggle switches between End to end and State accuracy', () => {
  const dom = installDOM();
  const root = dom.root(1120);
  const destroy = frontier.mount(root, data, makeStore());
  const toggle = root.querySelector('.chart-toggle');
  assert.ok(toggle);
  const buttons = toggle.querySelectorAll('button');
  assert.equal(buttons.length, 2);
  assert.equal(buttons[0].textContent, 'End to end');
  assert.equal(buttons[0].getAttribute('aria-pressed'), 'true');
  assert.equal(buttons[1].textContent, 'State accuracy');
  assert.equal(buttons[1].getAttribute('aria-pressed'), 'false');

  const firstPath = root.querySelector('.chart-frontier').getAttribute('d');
  buttons[1].dispatchEvent({ type: 'click' });
  assert.equal(buttons[0].getAttribute('aria-pressed'), 'false');
  assert.equal(buttons[1].getAttribute('aria-pressed'), 'true');
  const secondPath = root.querySelector('.chart-frontier').getAttribute('d');
  assert.notEqual(secondPath, firstPath);
  destroy();
});

test('outcomes preserves rank, seven-segment distinct outcomes, and computed caption', () => {
  const dom = installDOM();
  const root = dom.root(560);
  const destroy = outcomes.mount(root, data, makeStore());
  const rows = root.querySelectorAll('[data-row-model]');
  assert.deepEqual(
    rows.map((n) => n.getAttribute('data-row-model')),
    data.models.slice().sort((a, b) => a.rank - b.rank).map((m) => m.id),
  );
  for (const row of rows) {
    const model = data.models.find((m) => m.id === row.getAttribute('data-row-model'));
    const segments = row.querySelectorAll('[data-outcome]');
    assert.equal(segments.length, Object.values(model.outcomes).filter((n) => n > 0).length);
    for (const segment of segments) assert.ok(Number(segment.getAttribute('width')) > 0);
    for (const [key, label] of outcomes.OUTCOME_SEGMENTS) {
      assert.ok(row.getAttribute('aria-label').includes(`${label}: ${model.outcomes[key]}`));
    }
  }
  const count = data.models.filter((m) => m.outcomes.end_to_end > data.suite.slots_per_run / 2).length;
  assert.match(root.querySelector('.chart-caption').textContent, new RegExp(`Only ${count} of ${data.models.length}`));
  destroy();
});

test('anatomy displays 7 categories on mobile and 7 categories + 12 coupling families on desktop', () => {
  const dom = installDOM();
  for (const width of [328, 1120]) {
    const root = dom.root(width);
    const destroy = anatomy.mount(root, data, makeStore());
    const expected = data.categories.map((item) => (width < 640 ? (item.short || item.label) : item.label))
      .concat(width < 640 ? [] : data.families.map((item) => item.id));
    const actual = root.querySelectorAll('.chart-column-label').map((node) => node.children.map((line) => line.textContent).join(' '));
    assert.deepEqual(actual, expected);
    destroy();
  }
});

test('safety plot follows outcomes height only at side-by-side viewport breakpoint', () => {
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
    safetyRoot.querySelector('.chart-header').rect = { width: 544, height: 24 };
    safetyRoot.querySelector('.chart-sub').rect = { width: 544, height: 40 };
    safetyRoot.querySelector('.chart-legend').rect = { width: 544, height: 17 };
  };
  setSafetyChrome();
  const expected = () => Math.max(354, outcomesRoot.rect.height - (24 + 40 + 8 + 16 + 17 + 12));
  dom.resize(outcomesRoot, 544, 900);
  assert.equal(Number(safetyRoot.querySelector('svg').getAttribute('height')), expected());
  dom.setViewportWidth(899);
  dom.resize(outcomesRoot, 544, 900);
  assert.equal(Number(safetyRoot.querySelector('svg').getAttribute('height')), 354);
  dom.setViewportWidth(1120);
  dom.resize(outcomesRoot, 544, 980);
  assert.equal(Number(safetyRoot.querySelector('svg').getAttribute('height')), expected());
  destroySafety();
  destroyOutcomes();
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
