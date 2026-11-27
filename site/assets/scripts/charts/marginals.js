import { heatmap } from './heatmap.js';

const labels = { R1: 'Zero-trust', R2: 'Multi-affil.', R3: 'Hybrid cloud', R4: 'M&A' };

export function mount(root, data, store) {
  return heatmap(root, data, store, {
    title: 'By enterprise environment',
    subtitle: 'Clean solve rate across 24 scenarios in each of four enterprise environments.',
    empty: 'No full-suite environment solve rates have been published.',
    columns: (data.regimes ?? []).map(({ id, label }) => ({ id, title: label, short: labels[id] ?? id })),
    rates: (model) => model.marginals?.regime_g_srr,
  });
}
