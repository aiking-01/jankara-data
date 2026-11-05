import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { convertJoysound } from '../scripts/web/joysound.js';

const doc = JSON.parse(readFileSync(new URL('../source/joysound_prices.json', import.meta.url), 'utf8'));
const example = Object.values(doc.shops)
  .flatMap((shop) => shop.sections)
  .find((section) => section.rows.some((row) => /30分.*LAST/i.test(row.label)));
assert.ok(example, '公式初期データにLAST料金が含まれます');
const lastRow = example.rows.find((row) => /30分.*LAST/i.test(row.label));
const section = { ...example, rows: [lastRow] };

test('公式の単純な閉店時刻が分かればLASTの30分料金を変換', () => {
  const result = convertJoysound([section], '11:00～翌5:00');
  assert.ok(result.regular?.length);
  const plan = result.regular[0].plans[0];
  assert.equal(plan.hours, '18:00～5:00');
  assert.ok(plan.days.length);
});

test('営業時間が未確認・曜日別・24時間ならLASTを推測して金額表示しない', () => {
  for (const hours of ['', '24時間営業', '11:00～翌5:00 ※日祝23:00まで']) {
    const result = convertJoysound([section], hours);
    assert.equal(result.reason, 'no-readable-rates', hours);
  }
});

test('元データ全61店を引き続き変換し、既存の日時料金を保持', () => {
  for (const [id, shop] of Object.entries(doc.shops)) {
    const converted = convertJoysound(shop.sections, shop.opening_hours);
    assert.ok(converted.regular?.some((table) => table.plans.length), id);
  }
});
