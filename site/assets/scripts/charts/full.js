import { mount as pareto } from './pareto.js';
import { mount as safety } from './safety.js';
import { mount as categories } from './categories.js';
import { mount as marginals } from './marginals.js';

export function fullChartData({ confirmatory, categories = [], regimes = [] }) {
  const scenarios = confirmatory?.scenarios;
  const models = (confirmatory?.entries || []).map((entry) => {
    const analysis = entry.analysis ?? {};
    return {
      id: entry.id, model_id: entry.model_id, name: entry.name, rank: entry.rank,
      vendor: entry.vendor, vendor_label: entry.vendor_label,
      reasoning_effort: entry.reasoning_effort, transport: entry.transport, transport_id: entry.transport_id,
      scores: {
        safety_adjusted_score: entry.safety_adjusted_score_tenths / (10 * scenarios),
        g_srr: entry.resolved_slots / scenarios,
        og_total: entry.og_total, ab_total: entry.ab_total,
        macro_cat_acc: analysis.macro_cat_acc,
        category_pass_rates: analysis.category_pass_rates,
      },
      usage: {
        estimated_cost_usd: analysis.estimated_cost_pricing_sha256 ? analysis.estimated_cost_usd : undefined,
        total_tokens: analysis.total_tokens,
        mean_task_time_s: analysis.mean_task_time_s,
      },
      marginals: { regime_g_srr: analysis.regime_g_srr },
    };
  });
  return { models, scenarios, categories, regimes };
}

export function mountAnalysis(root, data, store, onSelect) {
  const chartData = fullChartData(data);
  const linkedStore = {
    ...store,
    set(patch) {
      if (patch.selected) {
        store.set({ ...patch, vendors: new Set(), query: '', hover: null });
        onSelect(patch.selected);
      } else {
        store.set(patch);
      }
    },
  };
  const panels = { pareto, safety, marginals, categories };
  return Object.entries(panels).map(([id, mount]) => mount(root.querySelector(`#chart-${id}`), chartData, linkedStore));
}
