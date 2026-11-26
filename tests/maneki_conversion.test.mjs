// まねきねこの変換（scripts/web/maneki.js）。tests/fixtures の固定データだけを読む
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import {
  EXTRACT_VERSION, commonLooks, convertManeki, manekiConverter, noticeReason, todayNumber,
} from '../scripts/web/maneki.js';

const { shops } = JSON.parse(readFileSync(new URL('./fixtures/maneki_tables.json', import.meta.url), 'utf8'));
const TODAY = '2026-11-26';

// 告知を残すようにした後の記録（update_maneki_prices.py の extract 2）
const record = (id, extra = {}) => ({
  tables: shops[id].tables,
  extract: EXTRACT_VERSION,
  pdfs: 1,
  notices: [],
  news: [],
  pages: [{ table: true, chars: 900, paths: 250, images: 4, image_area: 0.011, look: '8e8eccce8e8e480e' }],
  ...extra,
});
const convert = (info, options = {}) => convertManeki(info.tables, info, { today: TODAY, ...options });

test('告知のある記録で表を読める店は金額を出す', () => {
  const result = convert(record('mn-yachimata-store'));
  assert.equal(result.reason, undefined);
  const plans = result.regular[0].plans;
  assert.deepEqual(plans.map((plan) => plan.hours), ['12:00～18:00', '18:00～2:00']);
  assert.deepEqual(plans[0].days.find((row) => row.day === '平日').prices, { 会員: 143, 一般: 214 });
  assert.equal(result.minStay, 60);
});

test('告知を残す前の古い記録や、記録を渡さない呼び出しは読まない', () => {
  const { extract, ...old } = record('mn-yachimata-store');
  assert.equal(convert(old).reason, 'old-extract');
  assert.equal(convertManeki(shops['mn-yachimata-store'].tables).reason, 'old-extract');
});

test('料金表の PDF が 2 つ以上ある店、PDF がない店、閉店した店は読まない', () => {
  assert.equal(convert(record('mn-yachimata-store', { pdfs: 2 })).reason, 'several-pdfs');
  assert.equal(convert(record('mn-yachimata-store', { pdfs: 0 })).reason, 'no-pdf');
  assert.equal(convert(record('mn-yachimata-store', { status: 'closed', tables: [] })).reason, 'closed');
  assert.equal(convert(record('mn-yachimata-store', { status: 'several-pdfs', tables: [] })).reason, 'several-pdfs');
});

test('帯の最終受付は last_entry にする（京阪守口店、保土ケ谷駅前店）', () => {
  const moriguchi = convert(record('mn-keihan-moriguchi-store'));
  assert.deepEqual(moriguchi.regular[0].plans.map((plan) => [plan.hours, plan.last_entry]),
    [['11:00～18:00', ''], ['18:00～5:00', '翌4:00']]);
  const hodogaya = convert(record('mn-hodogaya-ekimae-store'));
  assert.deepEqual(hodogaya.regular[0].plans.map((plan) => plan.last_entry), ['', '翌3:59']);
});

test('読めない最終受付や、帯の金額の使い方を変えるフリータイムの注記は帯ごと読まない', () => {
  // 「※日・祝は【深夜フリータイム】の料金で18時から入室可」
  assert.equal(convert(record('mn-motoyawata-kitaguchi-store')).reason, 'band-note');
  // 「※フリータイム終了後は夜料金でのご延長がかかります。」
  assert.equal(convert(record('mn-ebisu-nishiguchi-store')).reason, 'band-note');
  // 最終受付を読めない形に書き換える
  const tables = JSON.parse(JSON.stringify(shops['mn-keihan-moriguchi-store'].tables));
  for (const page of tables) for (const token of page) if (token[0].startsWith('※最終受付')) token[0] = '※最終受付は混雑状況によります';
  assert.equal(convert({ ...record('mn-keihan-moriguchi-store'), tables }).reason, 'band-note');
});

test('表でないページの特別料金の告知（長岡駅前店 819-20.pdf の 1 ページ目）があれば読まない', () => {
  const notices = [{ page: 1, table: false, text: '11月20日(金)~ 特別料金での営業となり ます 。 お得な3時間パック室料込 アルコ ール飲み放題コ ー ス3500円(税込)' }];
  assert.equal(convert(record('mn-yachimata-store', { notices })).reason, 'notice');
});

test('期間の告知は今日から 21 日先までと重なるときだけ読まない（年をまたぐ期間）', () => {
  const notices = [{ page: 2, table: false, text: '年末年始特別料金のお知らせ 期間 12月29日(月)～1月3日(土)' }];
  const info = record('mn-yachimata-store', { notices });
  assert.equal(convert(info, { today: '2026-12-07' }).reason, undefined);
  assert.equal(convert(info, { today: '2026-12-08' }).reason, 'notice');
  assert.equal(convert(info, { today: '2027-01-03' }).reason, 'notice');
  assert.equal(convert(info, { today: '2027-01-04' }).reason, undefined);
});

test('期間の読めない告知は読まない。全店共通の断り書きは告知とみなさない', () => {
  const undated = [{ page: 1, table: true, text: '※年末年始は特別料金となります' }];
  assert.equal(convert(record('mn-yachimata-store', { notices: undated })).reason, 'notice-undated');
  const hedge = [{ page: 1, table: true, text: '※年末年始などの特別期間は料金が異なる場合がございます 。' }];
  assert.equal(convert(record('mn-yachimata-store', { notices: hedge })).reason, undefined);
});

test('店のページのお知らせも告知として見る。終わった年の期間は読む', () => {
  const news = [{ date: '2026-11-20', text: '12/29~1/3は年末年始特別料金での営業となります' }];
  assert.equal(convert(record('mn-yachimata-store', { news }), { today: '2026-12-20' }).reason, 'notice');
  const past = [{ date: '2025-12-01', text: '2025年12月29日~2026年1月3日は年末年始特別料金です' }];
  assert.equal(convert(record('mn-yachimata-store', { news: past }), { today: '2026-12-20' }).reason, undefined);
  assert.equal(convert(record('mn-yachimata-store', { news: [{ date: '', text: '臨時休業のお知らせ 12月1日' }] })).reason, 'notice');
});

test('途中で切った告知は読まない', () => {
  const notices = [{ page: 2, table: false, text: 'あ'.repeat(10), cut: true }];
  assert.equal(convert(record('mn-yachimata-store', { notices })).reason, 'notice-cut');
});

test('告知の日付の読み方', () => {
  const at = (text, today) => noticeReason([text], todayNumber(today));
  assert.equal(at('12月1日より料金改定いたします', TODAY), 'notice');
  assert.equal(at('2027年1月より料金改定', TODAY), null);
  assert.equal(at('2027年1月より料金改定', '2026-12-20'), 'notice');
  assert.equal(at('~11月30日まで特別料金', TODAY), 'notice');
  assert.equal(at('特別料金 12月1日 1月31日', '2026-12-20'), 'notice'); // 「~」が抜けても間の日を含める
  assert.equal(at('11月2日~4日<唐津くんち期間> 特別料金', TODAY), null);
  assert.equal(at('お盆特別料金 8/13~8/16', TODAY), null);
  assert.equal(at('まねきねこ最新モニター MASMO導入済!', TODAY), null);
});

test('変換する日は CONVERT_DATE で差し替えられる', () => {
  const saved = process.env.CONVERT_DATE;
  process.env.CONVERT_DATE = '2026-12-08';
  try {
    assert.equal(todayNumber(), Date.UTC(2026, 11, 8) / 86400000);
  } finally {
    if (saved === undefined) delete process.env.CONVERT_DATE;
    else process.env.CONVERT_DATE = saved;
  }
});

// 画像のページ。look は update_maneki_prices.py が残す見た目の指紋
const FLYER = '78f9717b71512533';
const flyerPage = (look) => ({ table: false, chars: 0, paths: 2, images: 2, image_area: 0.88, look });
const withPages = (id, extra) => record(id, { pages: [record(id).pages[0], ...extra] });

test('画像だけの告知のページ（1 店にしかない画像）は読まない', () => {
  const info = withPages('mn-yachimata-store', [flyerPage('e0daa4dce4da9201')]);
  assert.equal(convert(info).reason, 'image-page');
  assert.equal(convert(info, { commonLooks: commonLooks({ a: info }) }).reason, 'image-page');
});

test('多くの店にある同じ画像のページ（飲み放題の案内など）は読む', () => {
  const many = {};
  for (let i = 0; i < 12; i += 1) many[`mn-${i}`] = withPages('mn-yachimata-store', [flyerPage(i % 2 ? FLYER : '78f9717b71510533')]);
  const result = manekiConverter(many, { today: TODAY })(many['mn-0']);
  assert.equal(result.reason, undefined);
  // 10 店に満たなければ共通の案内とみなさない
  const few = Object.fromEntries(Object.entries(many).slice(0, 5));
  assert.equal(manekiConverter(few, { today: TODAY })(few['mn-0']).reason, 'image-page');
});

test('料金表に似た画像のページは、多くの店にあっても読まない（画像にした年末年始の料金表など）', () => {
  const tableLike = 'aecef6d29ae2c494';
  const many = {};
  for (let i = 0; i < 12; i += 1) {
    many[`mn-${i}`] = withPages('mn-yachimata-store', [flyerPage(tableLike)]);
    many[`mn-${i}`].pages[0].look = 'aecef6d29ae2c4d4'; // 料金表のページの見た目が近い
  }
  assert.equal(manekiConverter(many, { today: TODAY })(many['mn-0']).reason, 'image-page');
});

test('文字をアウトライン化したページや、料金表のページを覆う大きな画像があれば読まない', () => {
  const outlined = withPages('mn-yachimata-store', [{ table: false, chars: 0, paths: 1280, images: 2, image_area: 0.01, look: 'b4dad2d2d2a2c692' }]);
  assert.equal(convert(outlined).reason, 'image-page');
  const covered = record('mn-yachimata-store', { pages: [{ table: true, chars: 900, paths: 250, images: 5, image_area: 0.3, look: '8e8eccce8e8e480e' }] });
  assert.equal(convert(covered).reason, 'image-on-table');
  // 小さな画像（ロゴや QR コード）だけの表でないページは見ない
  const small = withPages('mn-yachimata-store', [{ table: false, chars: 0, paths: 3, images: 2, image_area: 0.02 }]);
  assert.equal(convert(small).reason, undefined);
});
