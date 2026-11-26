// 不自然な金額の店は変換で外し、ほかの店とチェーンの公開は止めない
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { implausiblePrice } from '../scripts/plausible.mjs';

const shop = (name, value) => ({
  regular: [{ band: '30分', plans: [{ name, hours: '', days: [{ day: '平日', prices: { 一般: value, 会員: null } }] }] }],
});

test('ふつうの金額の店はそのまま', () => {
  assert.equal(implausiblePrice(shop('30分', 380)), null);
  assert.equal(implausiblePrice(shop('フリータイム', 1500)), null);
  assert.equal(implausiblePrice({}), null);
});

test('30 分に 2000 円以上や小数があれば外す', () => {
  assert.match(implausiblePrice(shop('30分', 2400)), /2400/);
  assert.match(implausiblePrice(shop('30分', 380.5)), /380.5/);
});

test('フリータイムやパックが 100 円未満や 1 万円以上なら外す', () => {
  assert.ok(implausiblePrice(shop('3時間パック', 50)));
  assert.ok(implausiblePrice(shop('フリータイム', 12000)));
});

test('ヒトカラの表も確かめる', () => {
  assert.ok(implausiblePrice({ solo: shop('30分', -1).regular }));
});
