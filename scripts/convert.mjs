// 料金表を金額へ変換する。アプリに配るのは変換できた店の金額だけ
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { convertBigEcho } from './web/bigecho.js';
import { convertCotedazurShops } from './web/cotedazur.js';
import { convertManeki } from './web/maneki.js';
import { convertMegabig } from './web/megabig.js';
import { convertKanOcr } from './web/ocr.js';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const FIELDS = ['regular', 'solo', 'week', 'flat', 'minStay', 'alerts'];
// カラオケ館は料金表が画像なので、ocr.yml が読んだ結果から作る。ocr.json には他のチェーンの店も入っている
const TARGETS = {
  bigecho: ['chain_prices.json', (shop) => convertBigEcho(shop.sections, shop.hours)],
  maneki: ['maneki_prices.json', (shop) => convertManeki(shop.tables)],
  megabig: ['megabig_prices.json', (shop) => convertMegabig(shop.sections, shop.hours)],
  kan: ['ocr.json', (shop) => (shop.chain === 'カラオケ館' ? convertKanOcr(shop.reads) : { reason: 'other-chain' })],
};

function isEmpty(value) {
  return value === undefined || value === null || (Array.isArray(value) && !value.length);
}

mkdirSync(join(ROOT, 'build'), { recursive: true });
// コート・ダジュールは公式料金JSONを保持し、engine.js 用の通常料金とヒトカラ料金も作る。
const coteSource = JSON.parse(readFileSync(join(ROOT, 'source', 'cotedazur_prices.json'), 'utf8'));
const { shops: coteShops, rejected: coteFailures, accepted, original } =
  convertCotedazurShops(coteSource.shops);
for (const { id, reason } of coteFailures) {
  console.warn(`::warning::cotedazur/${id}: price schema is unsupported: ${reason}`);
}
writeFileSync(join(ROOT, 'build', 'cotedazur.json'), JSON.stringify({ updated_at: coteSource.updated_at, shops: coteShops }));
console.log(`cotedazur: ${accepted}/${original} shops (${coteFailures.length} unsupported)`);

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

// JOYJOYは公式ページに掲載された「～」付きの最低価格を保つ。通常料金へ
// 変換すると実額と誤認されるため、単位・時間帯・最低価格をそのまま配る。
const joyjoy = JSON.parse(readFileSync(join(ROOT, 'source', 'joyjoy_prices.json'), 'utf8'));
writeFileSync(join(ROOT, 'build', 'joyjoy.json'), JSON.stringify({
  updated_at: joyjoy.updated_at,
  shops: joyjoy.shops,
}));
console.log(`joyjoy: ${Object.keys(joyjoy.shops).length} 店 (公式最低料金)`);
