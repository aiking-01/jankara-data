// 料金表を金額へ変換する。アプリに配るのは変換できた店の金額だけ
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { convertBigEcho } from './web/bigecho.js';
import { convertManeki } from './web/maneki.js';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const FIELDS = ['regular', 'solo', 'week', 'flat', 'minStay', 'alerts'];
const TARGETS = {
  bigecho: ['chain_prices.json', (shop) => convertBigEcho(shop.sections, shop.hours)],
  maneki: ['maneki_prices.json', (shop) => convertManeki(shop.tables)],
};

function isEmpty(value) {
  return value === undefined || value === null || (Array.isArray(value) && !value.length);
}

mkdirSync(join(ROOT, 'build'), { recursive: true });
for (const [name, [file, convert]] of Object.entries(TARGETS)) {
  const source = JSON.parse(readFileSync(join(ROOT, 'source', file), 'utf8'));
  const shops = {};
  for (const [id, shop] of Object.entries(source.shops)) {
    const converted = convert(shop);
    if (converted.reason) continue;
    shops[id] = { checked_at: shop.checked_at };
    for (const field of FIELDS) if (!isEmpty(converted[field])) shops[id][field] = converted[field];
  }
  const total = Object.keys(source.shops).length;
  console.log(`${name}: ${Object.keys(shops).length} / ${total}`);
  writeFileSync(join(ROOT, 'build', `${name}.json`), JSON.stringify({ updated_at: source.updated_at, shops }));
}
