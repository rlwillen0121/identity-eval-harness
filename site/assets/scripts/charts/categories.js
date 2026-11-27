import { heatmap } from './heatmap.js';

export function mount(root, data, store) {
  return heatmap(root, data, store, {
    title: 'By IAM domain',
    subtitle: 'Pass rate among scenarios that exercise each domain.',
    empty: 'No full-suite domain pass rates have been published.',
    scrollLabel: 'IAM domains',
    columns: data.categories ?? [],
    rates: (model) => model.scores?.category_pass_rates,
  });
}
