import { date, endToEndPct, escapeHTML as esc, int } from './format.js';
import { vendorLogo } from './logos.js';

export function groupUpdates(data) {
  const modelsById = new Map(data.models.map((model) => [model.id, model]));
  const byDate = new Map();
  for (const update of data.history) {
    const published = update.date || '';
    if (!byDate.has(published)) byDate.set(published, []);
    for (const id of update.models) {
      const model = modelsById.get(id);
      if (model) byDate.get(published).push(model);
    }
  }
  const dates = [...byDate.keys()].sort((a, b) => b.localeCompare(a));
  const groups = dates.map((published) => ({
    date: published,
    models: byDate.get(published).sort((a, b) => a.rank - b.rank || a.name.localeCompare(b.name, 'en')),
  }));
  return { groups, earliestDate: dates.at(-1) ?? null };
}

export const VISIBLE_UPDATE_COUNT = 6;

export function mount(root, data, store) {
  const { groups, earliestDate } = groupUpdates(data);
  root.innerHTML = `
    <p class="section-lede">When each model's ${int(data.suite.scenarios)}-scenario run was first published.</p>
    <div class="updates-ledger">
      ${groups.map((group, index) => {
        const listId = `updates-models-${index}`;
        const hiddenCount = Math.max(0, group.models.length - VISIBLE_UPDATE_COUNT);
        return `<section class="updates-day" aria-label="Published ${esc(date(group.date))}">
          <div class="updates-date-block"><time class="updates-date" datetime="${esc(group.date)}">${esc(date(group.date))}</time>${group.date === earliestDate ? '<span class="updates-launch">Launch</span>' : ''}</div>
          <div class="updates-day-content">
          <ul class="updates-models" id="${listId}">${group.models.map((model, modelIndex) => `<li${modelIndex >= VISIBLE_UPDATE_COUNT ? ' hidden' : ''}><button class="update-model-button" type="button" data-model-id="${esc(model.id)}" aria-pressed="false" aria-label="Select ${esc(model.name)}, rank ${int(model.rank)}, ${endToEndPct(model.g_srr)} end to end"><span class="update-model-name">${vendorLogo(model.vendor, model.vendor_label)}${esc(model.name)}</span><span class="update-model-meta">#${int(model.rank)} · ${endToEndPct(model.g_srr)}</span></button></li>`).join('')}</ul>
            ${hiddenCount ? `<button class="updates-expand" type="button" data-hidden-count="${hiddenCount}" aria-controls="${listId}" aria-expanded="false">Show ${hiddenCount} more</button>` : ''}
          </div>
        </section>`;
      }).join('')}
    </div>`;

  function highlight() {
    const { hover, selected } = store.get();
    root.querySelectorAll('.update-model-button').forEach((button) => {
      const id = button.dataset.modelId;
      button.setAttribute('aria-pressed', String(id === selected));
      button.classList.toggle('is-selected', id === selected);
      button.classList.toggle('is-highlighted', id === hover);
      button.classList.toggle('is-receded', Boolean(hover) && id !== hover && id !== selected);
    });
  }

  function onClick(event) {
    const target = event.target instanceof Element ? event.target : null;
    const expand = target?.closest('.updates-expand');
    if (expand) {
      const expanded = expand.getAttribute('aria-expanded') === 'true';
      const day = expand.closest('.updates-day-content');
      const items = day.querySelectorAll('.updates-models li');
      items.forEach((item, index) => { item.hidden = expanded && index >= VISIBLE_UPDATE_COUNT; });
      expand.setAttribute('aria-expanded', String(!expanded));
      expand.textContent = expanded ? `Show ${expand.dataset.hiddenCount} more` : 'Show fewer';
      return;
    }
    const modelButton = target?.closest('.update-model-button');
    if (modelButton) store.set({ selected: modelButton.dataset.modelId });
  }

  function onEnter(event) {
    const target = event.target instanceof Element ? event.target : null;
    const button = target?.closest('.update-model-button');
    if (button && button !== event.relatedTarget?.closest?.('.update-model-button')) {
      store.set({ hover: button.dataset.modelId });
    }
  }

  function onLeave(event) {
    const target = event.target instanceof Element ? event.target : null;
    const button = target?.closest('.update-model-button');
    if (button && button !== event.relatedTarget?.closest?.('.update-model-button')
        && store.get().hover === button.dataset.modelId) {
      store.set({ hover: null });
    }
  }

  root.addEventListener('click', onClick);
  root.addEventListener('mouseover', onEnter);
  root.addEventListener('focusin', onEnter);
  root.addEventListener('mouseout', onLeave);
  root.addEventListener('focusout', onLeave);
  highlight();
  const unsubscribe = store.subscribe(highlight);
  return () => {
    unsubscribe();
    root.removeEventListener('click', onClick);
    root.removeEventListener('mouseover', onEnter);
    root.removeEventListener('focusin', onEnter);
    root.removeEventListener('mouseout', onLeave);
    root.removeEventListener('focusout', onLeave);
  };
}
