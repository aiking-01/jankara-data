import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { convertCotedazurShops } from '../scripts/web/cotedazur.js';

const data = JSON.parse(readFileSync(new URL('../source/cotedazur_prices.json', import.meta.url), 'utf8'));
const [id, first] = Object.entries(data.shops)[0];

test('1店舗だけ公式料金表の型が変わっても、正常店舗は変換する', () => {
  const records = {
    [id]: first,
    'cd-invalid': { shop_code: 99999, price_plan: [] },
  };
  const result = convertCotedazurShops(records, 0.5);
  assert.equal(result.accepted, 1);
  assert.equal(result.rejected.length, 1);
  assert.equal(result.rejected[0].id, 'cd-invalid');
  assert.ok(result.shops[id].regular.length > 0);
  assert.ok(result.shops[id].solo.length > 0);
});

test('公開料金が大量欠落するならfail-closedで配信を停止する', () => {
  const records = Object.fromEntries(Array.from({ length: 11 }, (_, i) => [
    `cd-${i}`, i === 0 ? first : { shop_code: i, price_plan: [] },
  ]));
  assert.throws(() => convertCotedazurShops(records), /safety threshold/);
  assert.throws(() => convertCotedazurShops({}), /safety threshold/);
});

test('公式JSONの全初期レコードが変換できる', () => {
  const result = convertCotedazurShops(data.shops);
  assert.equal(result.accepted, Object.keys(data.shops).length);
  assert.deepEqual(result.rejected, []);
});
