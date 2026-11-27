import { heatmap } from './heatmap.js';

const headings = {
  identity_resolution: ['Matching', 'identities'],
  human_lifecycle: ['Joiners, movers', 'and leavers'],
  nhi_agent_governance: ['Service accounts', 'and AI agents'],
  integration_fulfillment: ['App', 'provisioning'],
  access_policy: ['Access', 'policies'],
  cloud_entitlements: ['Cloud', 'permissions'],
  certification_remediation: ['Access', 'reviews'],
  change_cutover_safety: ['Safe', 'cutovers'],
};

export function mount(root, data, store) {
  return heatmap(root, data, store, {
    title: 'Which identity tasks did models handle well?',
    subtitle: 'Each column shows how often a model passed every check for that task in the scenarios that tested it.',
    empty: 'No full-suite results by identity task are available yet.',
    scrollLabel: 'IAM domains',
    columnWidth: 90,
    columns: (data.categories ?? []).map((col) => {
      const lines = headings[col.id];
      return lines ? { id: col.id, lines, title: lines.join(' ') } : col;
    }),
    rates: (model) => model.scores?.category_pass_rates,
  });
}
