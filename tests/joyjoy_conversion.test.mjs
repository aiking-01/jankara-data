// JOYJOY の変換（配る店を選ぶだけ）のテスト。tests/fixtures の見本だけを読む
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { activeNotice, convertDay, convertJoyjoyShops } from '../scripts/web/joyjoy.js';

const data = JSON.parse(readFileSync(new URL('./fixtures/joyjoy_prices.json', import.meta.url), 'utf8'));
const ids = (result) => Object.keys(result.shops).sort();

test('料金なしの店と、終わりの日付が読めない告知の店は配らない', () => {
  const result = convertJoyjoyShops(data.shops, '2026-11-26');
  // jj-Year-End-Store は 12/28～1/4 の告知がまだ終わっていないので配らない
  assert.deepEqual(ids(result), ['jj-Plan-Store', 'jj-Sample-Store']);
  const kinds = Object.fromEntries(result.rejected.map(({ id, kind }) => [id, kind]));
  assert.equal(kinds['jj-Withdrawn-Store'], '料金なし');
  assert.match(kinds['jj-Open-End-Store'], /期間の告知/);
});

test('年末年始の告知は、期間が終わるまで（始まりが先でも）配らない', () => {
  const shop = { 'jj-Year-End-Store': data.shops['jj-Year-End-Store'] };
  // 告知は 2026-12-28 ～ 2027-01-04
  for (const [day, shown] of [['2026-11-26', false], ['2026-12-06', false], ['2026-12-07', false],
    ['2026-12-31', false], ['2027-01-04', false], ['2027-01-05', true]]) {
    assert.equal(ids(convertJoyjoyShops(shop, day)).length === 1, shown, day);
  }
});

test('告知の期間の読み方', () => {
  assert.equal(activeNotice([], '2026-11-26'), null);
  assert.equal(activeNotice([{ text: 'x', from: null, until: '2026-11-25' }], '2026-11-26'), null);
  assert.ok(activeNotice([{ text: 'x', from: null, until: '2026-11-26' }], '2026-11-26'));
  assert.ok(activeNotice([{ text: 'x', from: '2026-12-01', until: null }], '2026-11-26'));
  assert.ok(activeNotice([{ text: 'x', from: '12/28', until: '2027-01-04' }], '2026-11-26'));
  assert.ok(activeNotice([{ text: 'x', from: '2026-12-01', until: '2026-02-30' }], '2026-11-26'));
});

test('配る店は元データと同じ形で、計算用の表は作らない', () => {
  const result = convertJoyjoyShops(data.shops, '2026-11-26');
  for (const [id, shop] of Object.entries(result.shops)) {
    assert.deepEqual(Object.keys(shop).sort(), ['checked_at', 'note', 'page', 'rates'], id);
    assert.deepEqual(shop.rates, data.shops[id].rates, id);
    for (const key of ['regular', 'solo', 'plans', 'week']) assert.equal(shop[key], undefined, id);
  }
});

test('最低価格がありえない金額の店は配らない', () => {
  const record = structuredClone(data.shops['jj-Sample-Store']);
  record.rates[0].windows[0].minimum_yen = 50;
  const result = convertJoyjoyShops({ 'jj-Sample-Store': record }, '2026-11-26');
  assert.deepEqual(result.shops, {});
  assert.equal(result.rejected[0].kind, '最低価格が不自然');
});

test('変換する日は JST の今日。CONVERT_TODAY で差し替えられる', () => {
  const night = Date.UTC(2026, 10, 25, 16, 30); // 2026-11-26 01:30 JST
  assert.equal(convertDay({}, night), '2026-11-26');
  assert.equal(convertDay({ CONVERT_TODAY: '2026-12-31' }, night), '2026-12-31');
  assert.equal(convertDay({ CONVERT_TODAY: '2026-13-01' }, night), '2026-11-26');
});
