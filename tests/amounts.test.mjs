// 配る直前の金額を確かめる。異常ならこの回は公開しない
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const read = (name) => JSON.parse(readFileSync(new URL(`../build/${name}.json`, import.meta.url), 'utf8')).shops;

const pricesOf = (name, slot) => Object.values(read(name))
  .flatMap((shop) => [...(shop.regular ?? []), ...(shop.solo ?? [])])
  .flatMap((table) => table.plans).filter((plan) => (plan.name === '30分') === slot)
  .flatMap((plan) => plan.days).flatMap((day) => Object.values(day.prices))
  .filter((value) => value !== null);

for (const name of ['bigecho', 'maneki', 'kan']) {
  test(`${name}: 30 分の金額として不自然な値がない`, () => {
    const prices = pricesOf(name, true);
    assert.ok(prices.length > 0);
    assert.ok(prices.every((value) => Number.isInteger(value) && value >= 0 && value < 2000));
  });
}

test('bigecho: フリータイムとパックの金額として不自然な値がない', () => {
  assert.ok(pricesOf('bigecho', false).every((value) => Number.isInteger(value) && value >= 100 && value < 10000));
});
