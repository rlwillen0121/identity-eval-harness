import assert from 'node:assert/strict';
import test from 'node:test';
import { createStore } from '../../../site/assets/js/main.js';

const initialState = () => ({
  sort: { key: 'rank', dir: 'asc' },
  vendors: new Set(['openai']),
  query: 'astra',
  selected: null,
  hover: null,
});

test('store skips semantically unchanged patches', () => {
  const store = createStore(initialState());
  let notifications = 0;
  store.subscribe(() => { notifications += 1; });

  store.set({});
  store.set({ query: 'astra' });
  store.set({ vendors: new Set(['openai']) });
  store.set({ sort: { key: 'rank', dir: 'asc' } });

  assert.equal(notifications, 0);
});

test('selecting a model clears filters and notifies', () => {
  const store = createStore(initialState());
  let notifications = 0;
  store.subscribe(() => { notifications += 1; });

  store.set({ selected: 'gpt-6-astra' });

  assert.equal(store.get().selected, 'gpt-6-astra');
  assert.equal(store.get().query, '');
  assert.deepEqual([...store.get().vendors], []);
  assert.equal(notifications, 1);
});

test('reselecting the same model clears new filters and still notifies', () => {
  const store = createStore({ ...initialState(), selected: 'gpt-6-astra' });
  let notifications = 0;
  store.subscribe(() => { notifications += 1; });
  store.set({ query: 'astra' });
  store.set({ vendors: new Set(['openai']) });
  notifications = 0;

  store.set({ selected: 'gpt-6-astra' });

  assert.equal(store.get().selected, 'gpt-6-astra');
  assert.equal(store.get().query, '');
  assert.deepEqual([...store.get().vendors], []);
  assert.equal(notifications, 1);
});

test('subscribers receive changed state and patch, and unsubscribe independently', () => {
  const store = createStore(initialState());
  const firstCalls = [];
  const secondCalls = [];
  const unsubscribe = store.subscribe((state, patch) => firstCalls.push({ state, patch }));
  store.subscribe((state, patch) => secondCalls.push({ state, patch }));

  store.set({ hover: 'gpt-6-astra' });
  store.set({ query: 'flash' });
  unsubscribe();
  store.set({ hover: null });

  assert.equal(firstCalls.length, 2);
  assert.deepEqual(firstCalls[0].patch, { hover: 'gpt-6-astra' });
  assert.equal(firstCalls[0].state.hover, 'gpt-6-astra');
  assert.deepEqual(firstCalls[1].patch, { query: 'flash' });
  assert.equal(firstCalls[1].state.query, 'flash');
  assert.equal(secondCalls.length, 3);
  assert.deepEqual(secondCalls[2].patch, { hover: null });
  assert.equal(secondCalls[2].state.hover, null);
});
