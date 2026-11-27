import { date, escapeHTML as esc, int } from './format.js';

export function mount(root, report) {
  if (!report?.published_on || !report.entries.length) {
    root.innerHTML = '<p class="section-lede">No dated publication entries yet.</p>';
    return;
  }

  root.innerHTML = `
    <div class="updates-ledger">
      <div class="updates-day">
        <div class="updates-date-block">
          <time class="updates-date" datetime="${esc(report.published_on)}">${esc(date(report.published_on))}</time>
        </div>
        <a class="update-model-button update-full-link" href="#leaderboard">
          <span class="update-model-name">${int(report.scenarios)}-scenario leaderboard</span>
          <span class="update-model-meta">${int(report.entries.length)} ranked</span>
        </a>
      </div>
    </div>
  `;
}
