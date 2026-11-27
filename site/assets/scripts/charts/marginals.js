import { heatmap } from './heatmap.js';

const headings = {
  R1: ['Regulated', 'access'],
  R2: ['People with', 'two roles'],
  R3: ['Cloud and', 'legacy apps'],
  R4: ['Company', 'mergers'],
};

export function mount(root, data, store) {
  root.classList.add('chart-environments');
  return heatmap(root, data, store, {
    title: 'How often did models solve the task?',
    subtitle: 'Each column shows the share of 24 scenarios solved safely. A student who leaves a campus job, for example, should keep access to classes.',
    empty: 'No full-suite results by setting are available yet.',
    scrollLabel: 'enterprise environments',
    columnWidth: 54,
    columns: (data.regimes ?? []).map(({ id, label }) => {
      const lines = headings[id] ?? [label ?? id];
      return { id, lines, title: lines.join(' ') };
    }),
    rates: (model) => model.marginals?.regime_g_srr,
  });
}
