const number = (options) => new Intl.NumberFormat('en-US', options);
const integer = number({ maximumFractionDigits: 0 });
const currency = number({ style: 'currency', currency: 'USD' });
const short = number({ notation: 'compact', maximumFractionDigits: 1 });
const duration = number({ maximumFractionDigits: 1 });
const known = (n) => typeof n === 'number' && Number.isFinite(n);
const monthNames = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

export function pct(rate, digits = 1) {
  return known(rate) ? number({ style: 'percent', minimumFractionDigits: digits, maximumFractionDigits: digits }).format(rate) : 'Not recorded';
}
export function int(n) { return known(n) ? integer.format(n) : 'Not recorded'; }
export function usd(n) { return known(n) ? currency.format(n) : 'Not recorded'; }
export function compact(n) { return known(n) ? short.format(n) : 'Not recorded'; }
export function seconds(n) { return known(n) ? `${duration.format(n)} s` : 'Not recorded'; }
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
