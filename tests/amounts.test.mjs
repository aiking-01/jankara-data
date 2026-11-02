// 配る直前の金額を確かめる。異常ならこの回は公開しない
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const read = (name) => JSON.parse(readFileSync(new URL(`../build/${name}.json`, import.meta.url), 'utf8')).shops;

for (const name of ['bigecho', 'maneki']) {
  test(`${name}: 30 分の金額として不自然な値がない`, () => {
    const prices = Object.values(read(name))
      .flatMap((shop) => [...(shop.regular ?? []), ...(shop.solo ?? [])])
      .flatMap((table) => table.plans).flatMap((plan) => plan.days).flatMap((day) => Object.values(day.prices))
      .filter((value) => value !== null);
    assert.ok(prices.length > 0);
    assert.ok(prices.every((value) => Number.isInteger(value) && value >= 0 && value < 2000));
  });
}
