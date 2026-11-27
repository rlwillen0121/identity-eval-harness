import { compact, duration, gSrrPct, num, pct, usd } from '../format.js';
import { chart, el, log, linear, scatter, stepObstacles, stepPath, svgCanvas, text, ticks, yAxis } from './svg.js';

const METRICS = [
  {
    id: 'cost', label: 'Cost', accessibleLabel: 'Estimated total cost', field: 'estimated_cost_usd',
    title: 'estimated cost', frontierUnit: 'estimated cost',
    axisLabel: 'Estimated total cost · USD (log scale)',
    tick: (value) => value < 1 ? usd(value) : `$${compact(value)}`,
    zeroNote: (models) => `$0 estimates for ${models.map((m) => m.name).join(', ')}`,
  },
  {
    id: 'tokens', label: 'Tokens', accessibleLabel: 'Total tokens', field: 'total_tokens',
    title: 'total tokens', frontierUnit: 'token total',
    axisLabel: 'Total tokens (log scale)',
    tick: compact,
    zeroNote: (models) => `zero token totals for ${models.map((m) => m.name).join(', ')}`,
  },
  {
    id: 'time', label: 'Time / task', accessibleLabel: 'Mean time per task', field: 'mean_task_time_s',
    title: 'time per task', frontierUnit: 'time per task',
    axisLabel: 'Mean time per task (log scale)',
    tick: formatTimeTick,
    zeroNote: (models) => `0s mean time per task for ${models.map((m) => m.name).join(', ')}`,
  },
];

const SCORE_METRICS = [
  { id: 'safety_adjusted_score', label: 'Safety-adjusted score', frontierLabel: 'score', axisLabel: 'Safety-adjusted score', tooltipLabel: (n) => `Safety-adjusted score (${n} scenarios)`, format: (m) => pct(m.scores?.safety_adjusted_score, 2) },
  { id: 'g_srr', label: 'Clean solve rate', frontierLabel: 'clean solve rate', axisLabel: 'Clean solve rate', tooltipLabel: (n) => `Clean solve rate (${n} scenarios)`, format: (m) => gSrrPct(m.scores?.g_srr) },
  { id: 'macro_cat_acc', label: 'Domain accuracy', frontierLabel: 'domain accuracy', axisLabel: 'Domain accuracy', tooltipLabel: (n) => `Domain accuracy averaged across ${n} scenarios`, format: (m) => pct(m.scores?.macro_cat_acc, 1) },
];

function formatTaskTime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return 'n/a';
  if (seconds === 0) return '0s';
  return seconds < 60 ? `${num(seconds, 1)} s` : duration(seconds);
}

function formatTimeTick(seconds) {
  if (seconds < 1) return `${num(seconds, 1)}s`;
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  return `${Math.round(seconds / 3600)} h`;
}

const valueOf = (model, field) => model.usage?.[field];

export function paretoFrontier(models, scoreMetric = 'safety_adjusted_score', xMetric = 'estimated_cost_usd') {
  const scoreOf = (m) => m.scores?.[scoreMetric];
  const measured = models
    .filter((m) => valueOf(m, xMetric) > 0 && Number.isFinite(valueOf(m, xMetric)) && Number.isFinite(scoreOf(m)))
    .slice()
    .sort((a, b) => valueOf(a, xMetric) - valueOf(b, xMetric) || scoreOf(b) - scoreOf(a) || a.rank - b.rank);
  let best = -Infinity;
  return measured.filter((m) => {
    const val = scoreOf(m);
    if (val <= best) return false;
    best = val;
    return true;
  });
}

function logCandidates(metric, min, max) {
  const values = new Set();
  // Time ticks use whole seconds, minutes or hours at the scale of the observations.
  const units = metric.id === 'time' ? [[1, 0, 60], [60, 60, 3600], [3600, 3600, Infinity]] : [[1, 0, Infinity]];
  for (const [unit, start, end] of units) {
    const low = Math.floor(Math.log10(min) - Math.log10(unit)) - 1;
    const high = Math.ceil(Math.log10(max) - Math.log10(unit)) + 1;
    for (let exponent = low; exponent <= high; exponent++) {
      for (const multiplier of [1, 2, 5]) {
        const value = unit * multiplier * (10 ** exponent);
        if (value > 0 && value >= start && value < end) values.add(value);
      }
    }
  }
  return [...values].sort((a, b) => a - b);
}

function logAxis(metric, observedMin, observedMax) {
  const candidates = logCandidates(metric, observedMin, observedMax);
  let min = candidates.filter((value) => value <= observedMin).at(-1) ?? observedMin;
  let max = candidates.find((value) => value >= observedMax) ?? observedMax;
  let selected = candidates.filter((value) => value >= min && value <= max);
  while (selected.length < 3) {
    const lower = candidates.filter((value) => value < min).at(-1);
    const upper = candidates.find((value) => value > max);
    if (!lower && !upper) break;
    if (!upper || (lower && Math.log(observedMin / lower) <= Math.log(upper / observedMax))) min = lower;
    else max = upper;
    selected = candidates.filter((value) => value >= min && value <= max);
  }
  if (!selected.length) selected = [observedMin, observedMax].filter((value, index, all) => index === 0 || value !== all[0]);
  if (selected.length > 5) {
    selected = Array.from({ length: 5 }, (_, index) => selected[Math.round(index * (selected.length - 1) / 4)]);
  }
  return { domain: [min, max], ticks: selected };
}

function scoreAxis(metric, models) {
  if (metric.id === 'macro_cat_acc') {
    const observedMin = Math.min(...models.map((model) => model.scores[metric.id]));
    const floor = Math.max(0, Math.floor(observedMin * 20) / 20);
    const min = floor >= 1 ? 0.95 : floor;
    return { min, count: 4, zoomed: min > 0 };
  }
  const min = Math.min(0, ...models.map((model) => Math.floor(4 * model.scores[metric.id]) / 4));
  return { min, count: Math.min(8, Math.max(1, Math.ceil((1 - min) * 4))), zoomed: false };
}

function labelPlan(models, frontier, metric, width) {
  const byRank = models.slice().sort((a, b) => (a.rank ?? Infinity) - (b.rank ?? Infinity));
  const cheapest = models.slice().sort((a, b) => valueOf(a, metric.field) - valueOf(b, metric.field))[0];
  const priority = [...new Set([
    ...byRank.slice(0, 2).map((model) => model.id),
    ...(cheapest ? [cheapest.id] : []),
    ...frontier.map((model) => model.id),
    ...byRank.map((model) => model.id),
  ])];
  if (width >= 850) return { ids: new Set(models.map((model) => model.id)), priority, mode: 'direct' };
  const directIds = new Set([
    ...byRank.slice(0, 2).map((model) => model.id),
    ...(cheapest ? [cheapest.id] : []),
  ]);
  const narrowIds = new Set([
    ...directIds,
    ...frontier.map((model) => model.id),
  ]);
  return { ids: narrowIds, directIds, priority, mode: 'frontier' };
}

export function mount(root, data, store) {
  const models = data.models || [];
  let metric = METRICS[0];
  let scoreMetric = SCORE_METRICS[0];
  const view = chart(root, 'Safety-adjusted score vs. estimated cost',
    'The line shows the best score at or below each estimated cost among plotted runs.',
    store, ({ width, canvas, tip }) => {
    const measured = models.filter((m) => Number.isFinite(m.scores?.[scoreMetric.id]) && Number.isFinite(valueOf(m, metric.field)));
    const positive = measured.filter((m) => valueOf(m, metric.field) > 0);
    const zero = measured.filter((m) => valueOf(m, metric.field) === 0);
    if (zero.length) {
      const note = document.createElement('p');
      note.className = 'chart-sub';
      const zeroSummary = metric.zeroNote ? metric.zeroNote(zero) : `zero values for ${zero.map((m) => m.name).join(', ')}`;
      note.textContent = `The logarithmic axis excludes ${zeroSummary}.`;
      canvas.append(note);
    }
    if (!positive.length) {
      if (!zero.length) {
        const p = document.createElement('p');
        p.className = 'status-note muted';
        p.textContent = `No full-suite configurations have a published ${metric.accessibleLabel.toLowerCase()} and score.`;
        canvas.append(p);
      }
      return () => {};
    }
    const plot = { left: 46, right: width - 18, top: 22, bottom: 300 };
    const values = positive.map((m) => valueOf(m, metric.field));
    const observedMin = Math.min(...values);
    const observedMax = Math.max(...values);
    const { domain, ticks: xTicks } = logAxis(metric, observedMin, observedMax);
    const x = log(domain, [plot.left + 8, plot.right - 8]);
    const yBounds = scoreAxis(scoreMetric, positive);
    const y = linear([yBounds.min, 1], [plot.bottom, plot.top + 24]);
    const frontier = paretoFrontier(positive, scoreMetric.id, metric.field);
    const svg = svgCanvas(canvas, width, 352, `Scatter plot of ${scoreMetric.label.toLowerCase()} across 96 scenarios against ${metric.accessibleLabel.toLowerCase()} for ${positive.length} configurations.`);
    yAxis(svg, plot, y, ticks(yBounds.min, 1, yBounds.count), (v) => pct(v, 0));
    svg.append(text(plot.left, 12, scoreMetric.axisLabel, { class: 'chart-axis-title' }));
    for (const v of xTicks) {
      svg.append(
        el('line', { x1: x(v), x2: x(v), y1: plot.bottom, y2: plot.bottom + 5, class: 'chart-axis' }),
        text(x(v), plot.bottom + 20, metric.tick(v), { 'text-anchor': 'middle', class: 'chart-tick' }),
      );
    }
    svg.append(text((plot.left + plot.right) / 2, 342, metric.axisLabel, { 'text-anchor': 'middle', class: 'chart-axis-title' }));
    const frontierPoints = frontier.map((m) => ({ x: x(valueOf(m, metric.field)), y: y(m.scores[scoreMetric.id]) }));
    const line = el('path', { d: stepPath(frontierPoints, plot.right), class: 'chart-frontier', stroke: 'var(--accent)' });
    svg.append(line);
    const ids = new Set(frontier.map((m) => m.id));
    const points = positive.map((m) => ({
      model: m,
      x: x(valueOf(m, metric.field)),
      y: y(m.scores[scoreMetric.id]),
      color: ids.has(m.id) ? 'var(--accent)' : 'var(--ink-2)',
      opacity: ids.has(m.id) ? 1 : 0.8,
      radius: ids.has(m.id) ? 5.5 : 5,
      lines: [
        [scoreMetric.tooltipLabel(data.scenarios ?? 96), scoreMetric.format(m)],
        ...SCORE_METRICS.filter((score) => score.id !== scoreMetric.id).map((score) => [score.tooltipLabel(data.scenarios ?? 96), score.format(m)]),
        ['Estimated total cost', usd(m.usage?.estimated_cost_usd)],
        ['Total tokens', compact(m.usage?.total_tokens)],
        ['Mean time per task', formatTaskTime(m.usage?.mean_task_time_s)],
      ],
    }));
    const plan = labelPlan(positive, frontier, metric, width);
    const sync = scatter(svg, points, plot, store, tip, root, plan.ids, {
      labelMode: plan.mode, labelPriority: plan.priority, directLabelIds: plan.directIds,
      obstacles: stepObstacles(frontierPoints, plot.right),
    });
    return (state) => {
      line.setAttribute('opacity', state.hover ? 0.35 : 1);
      sync(state);
    };
  });
  const updateChartCopy = () => {
    const title = view.header.querySelector('.chart-title');
    if (title) title.textContent = metric.id === 'cost' && scoreMetric.id === 'safety_adjusted_score'
      ? 'What does a higher score cost?'
      : `${scoreMetric.label} vs. ${metric.title}`;
    const caption = [...root.querySelectorAll('.chart-sub')].find((node) => node.parentNode === root);
    if (caption) {
      const frontierCopy = `The line shows the best ${scoreMetric.frontierLabel} at or below each ${metric.frontierUnit} among plotted runs.`;
      const currentModels = models.filter((model) => Number.isFinite(model.scores?.[scoreMetric.id]) &&
        Number.isFinite(valueOf(model, metric.field)) && valueOf(model, metric.field) > 0);
      const axis = currentModels.length ? scoreAxis(scoreMetric, currentModels) : { zoomed: false, min: 0 };
      caption.textContent = axis.zoomed
        ? `${frontierCopy} The vertical scale starts at ${pct(axis.min, 0)}.`
        : frontierCopy;
    }
  };
  const control = document.createElement('div');
  control.className = 'chart-controls';
  const switcher = document.createElement('div');
  switcher.className = 'chart-segmented chart-efficiency-switch';
  switcher.setAttribute('role', 'group');
  switcher.setAttribute('aria-label', 'Horizontal axis metric');
  const buttons = METRICS.map((nextMetric) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = nextMetric.label;
    btn.setAttribute('aria-label', nextMetric.accessibleLabel);
    btn.setAttribute('data-metric', nextMetric.id);
    btn.setAttribute('aria-pressed', String(nextMetric === metric));
    btn.addEventListener('click', () => {
      metric = nextMetric;
      buttons.forEach((button, index) => button.setAttribute('aria-pressed', String(METRICS[index] === metric)));
      updateChartCopy();
      view.redraw();
    });
    switcher.append(btn);
    return btn;
  });
  const scoreLabel = document.createElement('label');
  scoreLabel.className = 'chart-score-select';
  const scoreSelect = document.createElement('select');
  scoreSelect.setAttribute('aria-label', 'Vertical axis score metric');
  for (const option of SCORE_METRICS) {
    const item = document.createElement('option');
    item.value = option.id;
    item.textContent = option.label;
    scoreSelect.append(item);
  }
  scoreSelect.addEventListener('change', () => {
    scoreMetric = SCORE_METRICS.find((option) => option.id === scoreSelect.value) || SCORE_METRICS[0];
    updateChartCopy();
    view.redraw();
  });
  scoreLabel.append(scoreSelect);
  control.append(switcher, scoreLabel);
  view.header.append(control);
  const legend = document.createElement('div');
  legend.className = 'chart-outcome-key chart-resource-legend';
  legend.setAttribute('aria-label', 'Chart legend');
  const legendItem = document.createElement('span');
  const swatch = document.createElement('i');
  swatch.className = 'chart-resource-swatch';
  legendItem.append(swatch, document.createTextNode('Blue dots and the line show the best score reached so far.'));
  legend.append(legendItem);
  view.header.append(legend);
  updateChartCopy();
  return () => view.destroy();
}
