const number = (options) => new Intl.NumberFormat('en-US', options);
const integer = number({ maximumFractionDigits: 0 });
const currency = number({ style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 });
const short = number({ notation: 'compact', maximumFractionDigits: 1 });
const decimal = (digits = 1) => number({ minimumFractionDigits: digits, maximumFractionDigits: digits });
const known = (n) => typeof n === 'number' && Number.isFinite(n);
const monthNames = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

export const V52_INTERVAL_METHOD = 'outer union of a Rao–Wu stratified cluster bootstrap (10,000 replicates, Type-7 quantiles) and two-sided 95% Clopper–Pearson bounds';

export function pct(rate, digits = 1) {
  return known(rate)
    ? number({ style: 'percent', minimumFractionDigits: digits, maximumFractionDigits: digits }).format(rate)
    : '—';
}

export function gSrrPct(rate) {
  return pct(rate, rate === 0 ? 0 : 1);
}

export function int(n) {
  return known(n) ? integer.format(n) : '—';
}

export function usd(n) {
  return known(n) ? currency.format(n) : '—';
}

export function compact(n) {
  return known(n) ? short.format(n) : '—';
}

export function num(n, digits = 1) {
  return known(n) ? decimal(digits).format(n) : '—';
}

export function seconds(n) {
  return known(n) ? `${num(n, 1)} s` : '—';
}

export function tokPerSec(n) {
  return known(n) ? `${num(n, 1)} tok/s` : '—';
}

export function formatInterval(iv, asPct = true) {
  if (!iv || !known(iv.lo) || !known(iv.hi)) return '—';
  return asPct ? `${pct(iv.lo, 1)}–${pct(iv.hi, 1)}` : `#${num(iv.lo, 1)}–#${num(iv.hi, 1)}`;
}

export function date(iso) {
  const match = typeof iso === 'string' ? /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso) : null;
  if (!match) return 'Not recorded';
  const [, year, month, day] = match;
  const monthIndex = Number(month) - 1;
  const dayNumber = Number(day);
  const leapYear = Number(year) % 4 === 0 && (Number(year) % 100 !== 0 || Number(year) % 400 === 0);
  const monthDays = [31, leapYear ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (!monthNames[monthIndex] || dayNumber < 1 || dayNumber > monthDays[monthIndex]) return 'Not recorded';
  return `${dayNumber} ${monthNames[monthIndex]} ${year}`;
}

export function escapeHTML(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}
