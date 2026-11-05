// 料金表を金額へ変換する。アプリに配るのは変換できた店の金額だけ
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { convertBigEcho } from './web/bigecho.js';
import { convertCotedazur } from './web/cotedazur.js';
import { convertManeki } from './web/maneki.js';
import { convertKanOcr } from './web/ocr.js';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const FIELDS = ['regular', 'solo', 'week', 'flat', 'minStay', 'alerts'];
// カラオケ館は料金表が画像なので、ocr.yml が読んだ結果から作る。ocr.json には他のチェーンの店も入っている
const TARGETS = {
  bigecho: ['chain_prices.json', (shop) => convertBigEcho(shop.sections, shop.hours)],
  maneki: ['maneki_prices.json', (shop) => convertManeki(shop.tables)],
  kan: ['ocr.json', (shop) => (shop.chain === 'カラオケ館' ? convertKanOcr(shop.reads) : { reason: 'other-chain' })],
};

function isEmpty(value) {
  return value === undefined || value === null || (Array.isArray(value) && !value.length);
}

mkdirSync(join(ROOT, 'build'), { recursive: true });
// コート・ダジュールは公式料金JSONを保持し、engine.js 用の通常料金とヒトカラ料金も作る。
const coteSource = JSON.parse(readFileSync(join(ROOT, 'source', 'cotedazur_prices.json'), 'utf8'));
const coteShops = {};
const coteFailures = [];
for (const [id, shop] of Object.entries(coteSource.shops)) {
  try {
    // 公式サイトで一店舗だけ料金項目が変わっても、他の正常店舗まで失わない。
    const converted = convertCotedazur(shop);
    const { page, etag, last_modified, checked_at, ...officialData } = shop;
    coteShops[id] = { ...officialData, ...converted, checked_at };
  } catch (error) {
    coteFailures.push({ id, reason: String(error?.message ?? error) });
  }
}
const sourceCount = Object.keys(coteSource.shops).length;
const convertedCount = Object.keys(coteShops).length;
// 正常変換できた店舗が著しく減少したときは配信全体を止める。
if (!sourceCount || convertedCount < Math.ceil(sourceCount * 0.9)) {
  throw new Error(`cotedazur: converted ${convertedCount}/${sourceCount} below 90% safety threshold`);
}
for (const { id, reason } of coteFailures) {
  console.warn(`::warning::cotedazur/${id}: price schema is unsupported: ${reason}`);
}
writeFileSync(join(ROOT, 'build', 'cotedazur.json'), JSON.stringify({ updated_at: coteSource.updated_at, shops: coteShops }));
console.log(`cotedazur: ${convertedCount}/${sourceCount} shops (${coteFailures.length} unsupported)`);

for (const [name, [file, convert]] of Object.entries(TARGETS)) {
  const source = JSON.parse(readFileSync(join(ROOT, 'source', file), 'utf8'));
  const shops = {};
  let total = 0;
  for (const [id, shop] of Object.entries(source.shops)) {
    const converted = convert(shop);
    if (converted.reason !== 'other-chain') total += 1;
    if (converted.reason) continue;
    shops[id] = { checked_at: shop.checked_at };
    for (const field of FIELDS) if (!isEmpty(converted[field])) shops[id][field] = converted[field];
  }
  console.log(`${name}: ${Object.keys(shops).length} / ${total}`);
  writeFileSync(join(ROOT, 'build', `${name}.json`), JSON.stringify({ updated_at: source.updated_at, shops }));
}
