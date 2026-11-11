import test from 'node:test';
import assert from 'node:assert/strict';
import { compact, date, endToEndPct, escapeHTML, int, pct, safetyScreenDescription, safetyScreenLabels, seconds, usd } from '../../../site/assets/js/format.js';

test('percentage formatting covers bounds, small rates, negatives, and missing values', () => {
  assert.equal(pct(0), '0.0%');
  assert.equal(pct(1), '100.0%');
  assert.equal(pct(0.0009), '0.1%');
  assert.equal(pct(1, 0), '100%');
  assert.equal(pct(-0.25), '-25.0%');
  for (const value of [undefined, null, Number.NaN, Number.POSITIVE_INFINITY]) {
    assert.equal(pct(value), 'Not recorded');
  }
});

test('end-to-end rates omit decimals only at zero and safety descriptions use every supplied label', () => {
  assert.equal(endToEndPct(0), '0%');
  assert.equal(endToEndPct(0.911), '91.1%');
  assert.equal(endToEndPct(1), '100.0%');
  assert.equal(endToEndPct(undefined), 'Not recorded');
  assert.equal(pct(0), '0.0%', 'other percentage metrics retain their existing precision');

  const checks = [{ label: 'First check' }, { label: ' Second check ' }, { id: 'missing-label' }, null];
  assert.deepEqual(safetyScreenLabels(checks), ['First check', 'Second check']);
  assert.equal(safetyScreenDescription(checks), 'Passes all 2 safety and reliability checks simultaneously: First check; Second check.');
});

test('integer formatting handles zero, rounding, negatives, and missing values', () => {
  assert.equal(int(0), '0');
  assert.equal(int(1), '1');
  assert.equal(int(1.6), '2');
  assert.equal(int(-2), '-2');
  for (const value of [undefined, null, Number.NaN, Number.NEGATIVE_INFINITY]) {
    assert.equal(int(value), 'Not recorded');
  }
});

test('currency formatting handles zero, negative values, and missing values', () => {
  assert.equal(usd(0), '$0.00');
  assert.equal(usd(1), '$1.00');
  assert.equal(usd(1.5), '$1.50');
  assert.equal(usd(-1.25), '-$1.25');
  for (const value of [undefined, null, Number.NaN, Number.POSITIVE_INFINITY]) {
    assert.equal(usd(value), 'Not recorded');
  }
});

test('compact number formatting handles zero, large values, and missing values', () => {
  assert.equal(compact(0), '0');
  assert.equal(compact(1), '1');
  assert.equal(compact(1000), '1K');
  assert.equal(compact(-1000), '-1K');
  for (const value of [undefined, null, Number.NaN, Number.POSITIVE_INFINITY]) {
    assert.equal(compact(value), 'Not recorded');
  }
});

test('duration formatting handles zero, small and negative values, and missing values', () => {
  assert.equal(seconds(0), '0 s');
  assert.equal(seconds(1), '1 s');
  assert.equal(seconds(0.05), '0.1 s');
  assert.equal(seconds(-1.25), '-1.3 s');
  for (const value of [undefined, null, Number.NaN, Number.NEGATIVE_INFINITY]) {
    assert.equal(seconds(value), 'Not recorded');
  }
});

test('date formatting validates month and day bounds, including leap years', () => {
  assert.equal(date('2024-01-01'), '1 Jan 2024');
  assert.equal(date('2024-12-31'), '31 Dec 2024');
  assert.equal(date('2024-02-28'), '28 Feb 2024');
  assert.equal(date('2024-02-29'), '29 Feb 2024');
  assert.equal(date('2024-03-01'), '1 Mar 2024');
  assert.equal(date('1900-02-29'), 'Not recorded');
  assert.equal(date('2024-04-30'), '30 Apr 2024');
  for (const value of ['2024-00-01', '2024-13-01', '2024-01-00', '2024-04-31', '2024-2-01', 'not-a-date', undefined, null]) {
    assert.equal(date(value), 'Not recorded');
  }
});

test('HTML escaping encodes all markup delimiters', () => {
  assert.equal(escapeHTML(`<tag a="x" b='y'>&`), '&lt;tag a=&quot;x&quot; b=&#39;y&#39;&gt;&amp;');
  assert.equal(escapeHTML(0), '0');
  assert.equal(escapeHTML(1), '1');
  assert.equal(escapeHTML(-1), '-1');
  assert.equal(escapeHTML(undefined), 'undefined');
  assert.equal(escapeHTML(null), 'null');
});
