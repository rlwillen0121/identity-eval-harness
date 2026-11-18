export function setupTheme() {
  const button = document.getElementById('theme-toggle');
  if (!button) return;
  const os = window.matchMedia?.('(prefers-color-scheme: dark)');
  const isDark = () => (document.documentElement.dataset.theme
    ? document.documentElement.dataset.theme === 'dark' : Boolean(os?.matches));
  function render() {
    const dark = isDark();
    const label = `Switch to ${dark ? 'light' : 'dark'} theme`;
    button.setAttribute('aria-label', label);
    button.title = label;
    button.innerHTML = dark
      ? '<svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="3.5" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M10 1v2M10 17v2M1 10h2M17 10h2M3.6 3.6L5 5M15 15l1.4 1.4M3.6 16.4L5 15M15 5l1.4-1.4" stroke="currentColor" stroke-width="1.3"/></svg>'
      : '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M16.7 12.4A7 7 0 017.6 3.3a7 7 0 109.1 9.1z" fill="none" stroke="currentColor" stroke-width="1.3"/></svg>';
  }
  button.addEventListener('click', () => {
    const theme = isDark() ? 'light' : 'dark';
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem('identitybench-theme', theme); } catch { /* Theme still works without storage. */ }
    render();
  });
  os?.addEventListener?.('change', render);
  render();
}

// Chart labels are measured on a canvas, so wait briefly for the web fonts before drawing.
export async function fontsReady(limit = 1500) {
  if (!document.fonts?.load) return;
  const fonts = Promise.all([document.fonts.load('400 12px Inter'), document.fonts.load('400 10px "Geist Mono"')]);
  await Promise.race([fonts.catch(() => {}), new Promise((resolve) => setTimeout(resolve, limit))]);
}

export function reveal() {
  document.documentElement.classList.remove('is-loading');
  document.documentElement.classList.add('is-ready');
}
