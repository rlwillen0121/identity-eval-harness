function sameStoreValue(key, left, right) {
  if (key === 'vendors' && left instanceof Set && right instanceof Set) {
    return left.size === right.size && [...left].every((value) => right.has(value));
  }
  if (key === 'sort' && left && right && typeof left === 'object' && typeof right === 'object') {
    const keys = new Set([...Object.keys(left), ...Object.keys(right)]);
    return [...keys].every((name) => Object.is(left[name], right[name]));
  }
  return Object.is(left, right);
}

export function createStore(initial) {
  let state = initial;
  const listeners = new Set();
  return {
    get: () => state,
    set(patch) {
      const next = { ...state, ...patch };
      const selecting = Boolean(patch.selected);
      if (selecting) {
        next.query = '';
        next.vendors = new Set();
      }
      const repeatedSelection = selecting && patch.selected === state.selected;
      const unchanged = Object.keys(patch).every((key) => sameStoreValue(key, next[key], state[key]));
      if (unchanged && !repeatedSelection) return;
      state = next;
      [...listeners].forEach((fn) => fn(state, patch));
    },
    subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); },
  };
}
