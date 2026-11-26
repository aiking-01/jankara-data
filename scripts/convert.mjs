// 料金表を金額へ変換する。アプリに配るのは変換できた店の金額だけ
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { convertBigEcho } from './web/bigecho.js';
import { convertManeki } from './web/maneki.js';
import { convertMegabig } from './web/megabig.js';
import { convertKanShop } from './web/ocr.js';
import { convertCotedazurShops } from './web/cotedazur.js';
import { convertJoysound } from './web/joysound.js';
import { implausiblePrice } from './plausible.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const FIELDS = ['regular', 'solo', 'week', 'flat', 'minStay', 'alerts', 'source_rates'];
// カラオケ館は料金表が画像なので、ocr.yml が読んだ結果から作る。ocr.json には他のチェーンの店も入っている
const TARGETS = {
  joysound: ['joysound_prices.json', (shop) => convertJoysound(shop.sections, shop.opening_hours)],
  bigecho: ['chain_prices.json', (shop) => convertBigEcho(shop.sections, shop.hours)],
  maneki: ['maneki_prices.json', (shop) => convertManeki(shop.tables)],
  megabig: ['megabig_prices.json', (shop) => convertMegabig(shop.sections, shop.hours)],
  // 画像が変わったのに読めていない店と、14 日画像を確かめていない店は外す（ocr.js の convertKanShop）。
  // 1 店の読み取りで思わぬ例外が出ても、その店を配らないだけにして、ほかの店とほかのチェーンの変換は止めない
  kan: ['ocr.json', (shop) => {
    if (shop.chain !== 'カラオケ館') return { reason: 'other-chain' };
    try {
      return convertKanShop(shop);
    } catch (error) {
      console.warn(`::warning::kan: ${shop.url ?? ''} を変換できないので配りません (${error?.message ?? error})`);
      return { reason: 'error' };
    }
  }],
};

function isEmpty(value) {
  return value === undefined || value === null || (Array.isArray(value) && !value.length);
}


mkdirSync(join(ROOT, 'build'), { recursive: true });
// コート・ダジュールは公式料金JSONを保持し、engine.js 用の通常料金とヒトカラ料金も作る。
const coteSource = JSON.parse(readFileSync(join(ROOT, 'source', 'cotedazur_prices.json'), 'utf8'));
// 年末年始のような期間別の料金が出ると、ほとんどの店を読まなくなる。読めた店だけを配り、ほかのチェーンの変換は止めない。
// 読めない店は「料金表を見る」になるだけで、誤った金額は出ない（取得の失敗は validate_data.py が元データの店数で止める）
const { shops: coteShops, rejected: coteFailures, accepted, original } =
  Object.keys(coteSource.shops).length ? convertCotedazurShops(coteSource.shops, 0)
    : { shops: {}, rejected: [], accepted: 0, original: 0 };
const coteReasons = {};
for (const { reason } of coteFailures) {
  const kind = reason.replace(/^\d+: /, '');
  coteReasons[kind] = (coteReasons[kind] ?? 0) + 1;
}
for (const [reason, count] of Object.entries(coteReasons)) {
  console.warn(`::warning::cotedazur: ${count} shops not converted: ${reason}`);
}
for (const [id, shop] of Object.entries(coteShops)) {
  const odd = implausiblePrice(shop);
  if (odd) {
    console.warn(`::warning::cotedazur/${id}: 金額が不自然なので配りません (${odd})`);
    delete coteShops[id];
  }
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
    const odd = implausiblePrice(converted);
    if (odd) {
      console.warn(`::warning::${name}/${id}: 金額が不自然なので配りません (${odd})`);
      continue;
    }
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

// ROUND1 の B-30 画像は通常のカラオケ館 OCR とは別形式。
// 目視確認・画像SHA256照合済みの店舗だけ配信する。金額未確認の初期状態で
// 空の round1.json を配信して「対応店舗あり」と誤認させない。
const round1Path = join(ROOT, 'source', 'round1_b30.json');
if (existsSync(round1Path)) {
  const data = JSON.parse(readFileSync(round1Path, 'utf8'));
  if (data.schema_version !== 1 || !Number.isInteger(data.updated_at) ||
      !data.shops || typeof data.shops !== 'object' || Array.isArray(data.shops)) {
    throw new Error('source/round1_b30.json: invalid format');
  }
  const registry = JSON.parse(readFileSync(join(ROOT, 'chains.json'), 'utf8'));
  const registered = new Map((Array.isArray(registry.shops) ? registry.shops : Object.values(registry.shops))
    .map((shop) => [shop.id, shop]));
  const shops = {};
  for (const [id, shop] of Object.entries(data.shops)) {
    if (!/^r1-[A-Za-z0-9_-]+$/.test(id) || registered.get(id)?.chain !== 'ROUND1') {
      throw new Error(`source/round1_b30.json: unknown ROUND1 shop ${id}`);
    }
    if (!Number.isInteger(shop.checked_at) || !Array.isArray(shop.regular) ||
        shop.regular.length !== 1 || shop.regular[0]?.band !== '30分' ||
        !Array.isArray(shop.regular[0]?.plans) || !shop.regular[0].plans.length) {
      throw new Error(`source/round1_b30.json: invalid regular table for ${id}`);
    }
    for (const plan of shop.regular[0].plans) {
      if (plan.name !== '30分' || typeof plan.hours !== 'string' ||
          !Array.isArray(plan.days) || plan.days.length !== 5) {
        throw new Error(`source/round1_b30.json: invalid plan for ${id}`);
      }
      for (const row of plan.days) {
        if (!['日祝', '土', '金祝前', '月火水木', '平日'].includes(row.day) ||
            !row.prices || typeof row.prices !== 'object' || Array.isArray(row.prices) ||
            Object.entries(row.prices).some(([customer, price]) =>
              !['学生会員', '学生', 'シニア', '一般'].includes(customer) ||
              !Number.isInteger(price) || price < 30 || price > 2000)) {
          throw new Error(`source/round1_b30.json: invalid day prices for ${id}`);
        }
      }
    }
    shops[id] = { checked_at: shop.checked_at, regular: shop.regular };
  }
  if (Object.keys(shops).length) {
    writeFileSync(join(ROOT, 'build', 'round1.json'), JSON.stringify({
      updated_at: data.updated_at, shops,
    }));
    console.log(`round1: ${Object.keys(shops).length} verified shops`);
  }
}
