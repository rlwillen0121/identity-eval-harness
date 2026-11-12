// Applies a stored theme before first paint. Loaded as a classic, render-blocking script so the
// page never flashes the wrong theme; kept external so the CSP needs no inline-script allowance.
try {
  const theme = localStorage.getItem('identitybench-theme');
  if (theme === 'light' || theme === 'dark') document.documentElement.dataset.theme = theme;
} catch {
  // Storage can be unavailable (private windows, blocked site data); the OS theme still applies.
}

// Hold the data-driven sections back until the results are in place (see main.js), so the
// headline is never seen changing. The timer is a safety net if the module fails to run.
document.documentElement.classList.add('is-loading');
setTimeout(() => document.documentElement.classList.remove('is-loading'), 4000);
