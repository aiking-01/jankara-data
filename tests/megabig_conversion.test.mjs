// メガビッグの変換を見本（tests/fixtures/megabig_prices.json）で確かめる。公式データ（source/）は読まない
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { convertMegabig, jstDay, noticeWindows } from '../scripts/web/megabig.js';

const data = JSON.parse(readFileSync(new URL('./fixtures/megabig_prices.json', import.meta.url), 'utf8')).shops;
const TODAY = '2026-11-26';
const copy = (value) => JSON.parse(JSON.stringify(value));
const convert = (shop, today = TODAY) => convertMegabig(shop.sections, shop.hours, { ...shop, today });
const plans = (result, band) => (result.regular ?? []).filter((table) => table.band === band).flatMap((table) => table.plans);
const soloPlans = (result, band) => (result.solo ?? []).filter((table) => table.band === band).flatMap((table) => table.plans);
const plan = (result, name, band = 'フリータイム') => plans(result, band).find((item) => item.name === name);
const row = (item, day) => item.days.find((entry) => entry.day === day);
const reasons = (result) => Object.fromEntries((result.dropped ?? []).map((item) => [item.plan, item.reason]));
// 店の表のうち時間帯の欄が text で始まるもの
const section = (shop, text) => shop.sections.find((item) => item.time.startsWith(text));

test('見本の 11 店はすべて変換できる', () => {
  for (const [id, shop] of Object.entries(data)) {
    const result = convert(shop);
    assert.equal(result.reason, undefined, `${id}: ${result.reason}`);
    assert.ok(plans(result, '30分').length >= 2, id);
  }
});

test('最終受付は括弧付きの表記と「受付時間」から読む', () => {
  assert.equal(plan(convert(data['mb-0907']), '昼3時間パック').last_entry, '16:00');
  assert.equal(plan(convert(data['mb-0907']), '昼フリータイム').last_entry, '15:00');
  assert.equal(plan(convert(data['mb-1386']), '3時間パック').last_entry, '16:00');
  assert.equal(plan(convert(data['mb-1101']), '昼フリータイム').last_entry, '14:59');
  assert.equal(plan(convert(data['mb-1379']), '朝フリータイム').last_entry, '11:00');
  assert.equal(plan(convert(data['mb-1379']), '夕方フリータイム').last_entry, '18:00');
  assert.equal(plan(convert(data['mb-0907']), '深夜フリータイム').last_entry, '');
  // 最終受付が書いてあり、ほかの理由で外れないプランはすべて読める（前の変換は括弧付きの表記と「受付時間」を読めなかった）
  const filled = Object.fromEntries(Object.entries(data).map(([id, shop]) =>
    [id, plans(convert(shop), 'フリータイム').filter((item) => item.last_entry).length]));
  assert.deepEqual(filled, {
    'mb-0907': 4, 'mb-0937': 3, 'mb-1101': 2, 'mb-1379': 3, 'mb-1383': 3, 'mb-1386': 4,
    'mb-1389': 0, 'mb-1405': 2, 'mb-1406': 3, 'mb-1434': 3, 'mb-1434-summer': 3,
  });
});

test('曜日で変わる最終受付や、2 つある最終受付のプランは出さない', () => {
  const shop = copy(data['mb-0907']);
  section(shop, '昼3時間パック').note = '最終受付 平日16:00／土日祝15:00';
  section(shop, '昼フリータイム').note = '最終受付（15:00） 最終受付（14:00）';
  const result = convert(shop);
  assert.equal(plan(result, '昼3時間パック'), undefined);
  assert.equal(plan(result, '昼フリータイム'), undefined);
  assert.equal(reasons(result)['昼3時間パック'], 'last-entry');
  assert.equal(reasons(result)['昼フリータイム'], 'last-entry-varies');
  assert.ok(plan(result, '夕方フリータイム'));
});

test('受付の始まりが時間帯の始まりと違うプランは出さない', () => {
  const shop = copy(data['mb-1379']);
  section(shop, '夕方フリータイム').note = '・受付時間 （16:00-18:00）';
  assert.equal(plan(convert(shop), '夕方フリータイム'), undefined);
});

test('「-」だけの列はその曜日を料金なしにし、一部だけ「-」の表や読めないセルの表は出さない', () => {
  const shop = copy(data['mb-0907']);
  for (const values of section(shop, '昼3時間パック').rows) values[1] = '-';
  section(shop, '昼フリータイム').rows[0][1] = '-';
  section(shop, '夕方フリータイム').rows[0][2] = '980円※2';
  const result = convert(shop);
  const pack = plan(result, '昼3時間パック');
  assert.deepEqual(row(pack, '平日').prices, {});
  assert.equal(row(pack, '土日祝').prices.一般, 1800);
  assert.equal(reasons(result)['昼フリータイム'], 'partial-dash');
  assert.equal(reasons(result)['夕方フリータイム'], 'price-cell');
});

test('取得で捨てた表は警告の一覧に出す', () => {
  const shop = { ...copy(data['mb-0907']), skipped: [{ time: '早朝パック OPEN〜12:00', title: '早朝パック', reason: 'price-cell' }] };
  assert.equal(reasons(convert(shop))['早朝パックOPEN〜12:00'], 'price-cell');
});

test('備考に曜日限定の時刻があるプランは出さない。「料金」の「金」は曜日にしない', () => {
  const result = convert(data['mb-1389']);
  assert.equal(reasons(result)['フリータイム'], 'note-days'); // 土・日・祝は19:00まで
  assert.equal(plans(result, '30分').length, 2); // 「…御利用頂く料金となります」の表は残る
  const early = convert(data['mb-0937']);
  assert.equal(reasons(early)['早得パック'], 'unsupported-name');
  const shop = copy(data['mb-0907']);
  section(shop, '夕方フリータイム').note = 'アルコール・ソフトドリンク飲み放題込み 追加料金なし 金額は税込';
  assert.ok(plan(convert(shop), '夕方フリータイム'));
  section(shop, '夕方フリータイム').note = '金曜は21:00まで';
  assert.equal(plan(convert(shop), '夕方フリータイム'), undefined);
});

test('列の曜日と備考の曜日が食い違う表は、同じ列の表ごと出さない', () => {
  const result = convert(data['mb-1434']);
  assert.equal(reasons(result)['深夜フリータイム'], 'days-conflict'); // 列は「金・土・祝」、備考は「金・土・祝前は23:00～5:00」
  assert.equal(reasons(result)['夜ロングフリータイム'], 'days-conflict'); // 同じ「日〜木／金・土・祝」の列
  assert.ok(plan(result, '昼3時間パック'));
  assert.equal(plans(result, '30分').length, 2);
});

test('プラン名は時間帯の欄の先頭語から取る', () => {
  const shop = copy(data['mb-1389']);
  for (const item of shop.sections) item.note = 'アルコール・ソフトドリンク飲み放題付き';
  const result = convert(shop);
  assert.ok(plan(result, 'おはカラ'));
  assert.equal(plan(result, 'おはカラ').last_entry, '12:00');
  assert.equal(plans(result, 'フリータイム').filter((item) => item.name === 'フリータイム').length, 1);
  const pack = plan(result, '3時間パック'); // 平日は「-」
  assert.deepEqual(row(pack, '平日').prices, {});
  assert.equal(row(pack, '土日祝').prices.一般, 1580);
});

test('30分毎の 1 人の追加料金は、書いてある 30 分の表の書いてある曜日だけに足す', () => {
  const result = convert(data['mb-1386']);
  const [day, night] = plans(result, '30分');
  const [solo, ...rest] = soloPlans(result, '30分');
  assert.equal(rest.length, 0); // 夜の 30 分には追加料金の記載がない
  assert.equal(solo.hours, day.hours);
  assert.equal(row(solo, '平日').prices.一般, row(day, '平日').prices.一般);
  assert.equal(row(solo, '土日祝').prices.一般, row(day, '土日祝').prices.一般 + 100);
  assert.ok(night);
  // パック・フリータイムは 30 分毎の加算から 1 人の金額を作らない。土日祝の料金は出さない
  const pack = plan(result, '3時間パック');
  assert.deepEqual(row(pack, '土日祝').prices, {});
  assert.equal(row(pack, '平日').prices.一般, 1240);
  assert.deepEqual(soloPlans(result, 'フリータイム'), []);
});

test('1 回の追加料金は列の曜日の集まりと一致するときだけ足す', () => {
  const result = convert(data['mb-1383']);
  const pack = plan(result, '昼3時間パック');
  const solo = soloPlans(result, 'フリータイム').find((item) => item.name === '昼3時間パック');
  assert.equal(row(solo, '平日').prices.一般, row(pack, '平日').prices.一般 + 200);
  assert.equal(row(solo, '土日祝').prices.一般, row(pack, '土日祝').prices.一般 + 400);
  // 30 分の「（土・日・祝）+70円」は 30 分ごとか 1 回か分からないので、土日祝の 1 人の行は空
  const slot = soloPlans(result, '30分')[0];
  assert.deepEqual(row(slot, '土日祝').prices, {});
  assert.equal(row(slot, '平日').prices.一般, row(plans(result, '30分')[0], '平日').prices.一般);
  const tenma = convert(data['mb-1406']);
  const free = soloPlans(tenma, 'フリータイム').find((item) => item.name === '昼フリータイム');
  assert.equal(row(free, '土日祝').prices.一般, row(plan(tenma, '昼フリータイム'), '土日祝').prices.一般 + 300);
  assert.deepEqual(row(soloPlans(tenma, '30分')[0], '土日祝').prices, {});
});

test('「2名様以上の料金」の店は、フリータイム・パックを出さず 1 人の表も作らない', () => {
  const result = convert(data['mb-1389']);
  assert.deepEqual(plans(result, 'フリータイム'), []);
  assert.deepEqual(result.solo, []);
  assert.equal(reasons(result)['おはカラ'], 'solo-unknown-shop');
  // 1 つの表にだけ書いてあっても店全体に効く
  const shop = copy(data['mb-0907']);
  section(shop, 'OPEN').note = '上記は2名様以上で御利用頂く料金となります';
  assert.deepEqual(plans(convert(shop), 'フリータイム'), []);
});

test('読めない 1 人の文がある表は、1 人の金額を決めない', () => {
  const shop = copy(data['mb-0907']);
  section(shop, '昼3時間パック').note = '最終受付（16:00） 1名様のご利用は別料金となります';
  const result = convert(shop);
  assert.equal(plan(result, '昼3時間パック'), undefined);
  assert.equal(reasons(result)['昼3時間パック'], 'solo-unknown');
});

test('過ぎた期間料金の告知は無視し、今日から 21 日先までと重なる告知は店ごと出さない', () => {
  const summer = data['mb-1434-summer']; // 「8月1日(土)～9月6日(日)は夏季料金」
  assert.equal(convert(summer, '2026-11-26').reason, undefined);
  assert.equal(convert(summer, '2026-08-20').reason, 'period-notice');
  assert.equal(convert(summer, '2026-07-15').reason, 'period-notice'); // 21 日以内に始まる
  const ahead = convert(summer, '2026-07-01');
  assert.equal(ahead.reason, undefined);
  assert.deepEqual(ahead.alerts, ['夏季']);
});

test('年末年始の告知: 終わりが読めなければ出さない。年をまたぐ期間の年を決める', () => {
  const shop = copy(data['mb-0907']);
  shop.notices = [...shop.notices, '以下の表示料金は11月27日(金)までの料金となります。 11月28日(土)～は年末年始料金を導入させていただいております。 ＜年末年始期間の料金はこちら＞'];
  assert.equal(convert(shop).reason, 'period-notice');

  shop.notices = ['12月28日(月)～1月4日(月)は年末年始料金となります'];
  const before = convert(shop);
  assert.equal(before.reason, undefined);
  assert.deepEqual(before.alerts, ['年末年始']);
  assert.equal(convert(shop, '2026-12-10').reason, 'period-notice');
  assert.equal(convert(shop, '2027-01-03').reason, 'period-notice');
  assert.equal(convert(shop, '2027-01-10').reason, undefined);

  shop.notices = ['12月28日(火)～1月4日(月)は年末年始料金となります']; // 曜日がどの年にも合わない
  assert.equal(convert(shop).reason, 'period-notice');
  shop.notices = ['年末年始は特別料金となります'];
  assert.equal(convert(shop).reason, 'period-notice');
  shop.notices = ['12/30～1/3は特別料金'];
  assert.equal(convert(shop, '2026-11-26').reason, undefined);
  assert.equal(convert(shop, '2026-12-20').reason, 'period-notice');
});

test('期間の読み方', () => {
  const today = jstDay('2026-11-26');
  const day = (text) => jstDay(text);
  assert.deepEqual(noticeWindows('8月1日(土)～9月6日(日)は夏季料金', today), [[day('2026-08-01'), day('2026-09-06')]]);
  assert.deepEqual(noticeWindows('表示料金は11月27日(木)までの料金となります', today), [[day('2025-11-28'), Infinity]]);
  assert.deepEqual(noticeWindows('12月31日は特別料金', today), [[day('2026-12-31'), day('2026-12-31')]]);
  assert.deepEqual(noticeWindows('1月5日まで期間限定料金', today), [[-Infinity, day('2027-01-05')]]);
  assert.deepEqual(noticeWindows('12月31日(月)', today), [null]);
});

test('料金の PDF・画像へのリンク、税別の表、表の外の曜日の条件があれば店ごと出さない', () => {
  const shop = copy(data['mb-0907']);
  assert.equal(convert({ ...shop, links: ['https://megakara.jp/wp-content/uploads/2026/08/0827_1434.pdf'] }).reason, 'price-link');
  assert.equal(convert({ ...shop, links: ['https://megakara.jp/news/5778/'] }).reason, undefined);
  assert.equal(convert({ ...shop, notices: ['30分料金（税別）'] }).reason, 'tax-excluded');
  assert.equal(convert({ ...shop, notices: ['土日祝は2時間制'] }).reason, 'notice-days');
  assert.equal(convert({ ...shop, unread_tables: 1 }).reason, 'unread-table');
});

test('料金表がない記録は料金を出さない', () => {
  const result = convertMegabig([], '9:00～5:00', { missing: 'no-price-table', today: TODAY });
  assert.equal(result.reason, 'missing-no-price-table');
});

test('変換する日は CONVERT_TODAY で差し替えられる', () => {
  const saved = process.env.CONVERT_TODAY;
  try {
    process.env.CONVERT_TODAY = '2026-08-20';
    assert.equal(jstDay(), jstDay('2026-08-20'));
    const summer = data['mb-1434-summer'];
    assert.equal(convertMegabig(summer.sections, summer.hours, summer).reason, 'period-notice');
  } finally {
    if (saved === undefined) delete process.env.CONVERT_TODAY;
    else process.env.CONVERT_TODAY = saved;
  }
});
