import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { convertJoysound, convertJoysoundShop, drinkRule } from '../scripts/web/joysound.js';

const doc = JSON.parse(readFileSync(new URL('./fixtures/joysound_prices.json', import.meta.url), 'utf8'));
const example = Object.values(doc.shops)
  .flatMap((shop) => shop.sections)
  .find((section) => section.rows.some((row) => /30分.*LAST/i.test(row.label)));
assert.ok(example, '公式初期データにLAST料金が含まれます');
const lastRow = example.rows.find((row) => /30分.*LAST/i.test(row.label));
const section = { ...example, rows: [lastRow] };

test('公式の単純な閉店時刻が分かればLASTの30分料金を変換', () => {
  const result = convertJoysound([section], '11:00～翌5:00');
  assert.ok(result.regular?.length);
  const plan = result.regular[0].plans[0];
  assert.equal(plan.hours, '18:00～5:00');
  assert.ok(plan.days.length);
});

test('営業時間が未確認・曜日別・24時間ならLASTを推測して金額表示しない', () => {
  for (const hours of ['', '24時間営業', '11:00～翌5:00 ※日祝23:00まで']) {
    const result = convertJoysound([section], hours);
    assert.equal(result.reason, 'no-readable-rates', hours);
  }
});

test('見本の店を引き続き変換し、既存の日時料金を保持', () => {
  for (const [id, shop] of Object.entries(doc.shops)) {
    const converted = convertJoysound(shop.sections, shop.opening_hours);
    assert.ok(converted.regular?.some((table) => table.plans.length), id);
  }
});

// ---- 状態・期間告知・曜日・飲み物（v2 の記録。tests/fixtures/joysound_v2.json は公式ページの抜粋から作った） ----

const v2 = JSON.parse(readFileSync(new URL('./fixtures/joysound_v2.json', import.meta.url), 'utf8')).shops;
const kanayama = v2['js-joysound-kanayama'];
const aobadai = v2['js-joysound-aobadai'];
const jst = (text) => Date.parse(`${text}+09:00`) / 1000;

// 配信中のアプリ（2026-11 まで）の engine.js の pickDayRow と同じ選び方。行が 1 つなら曜日を見ずにその行を使う
const WEEKDAY = { 日: 0, 月: 1, 火: 2, 水: 3, 木: 4, 金: 5, 土: 6 };
function deployedPickDayRow(plan, date, holidays = new Set()) {
  const day = date.getDay();
  const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  const next = new Date(date.getFullYear(), date.getMonth(), date.getDate() + 1);
  const matches = (label) => {
    const tokens = label.match(/祝前|[月火水木金土日祝]/g);
    if (!tokens || tokens.join('') !== label) return null;
    return tokens.some((token) => (token === '祝' ? holidays.has(iso(date))
      : token === '祝前' ? holidays.has(iso(next)) : WEEKDAY[token] === day));
  };
  if (plan.days.length === 1) return plan.days[0];
  const weekdayRow = plan.days.find((row) => row.day === '平日');
  for (const row of plan.days) {
    if (row !== weekdayRow && matches(row.day)) return row;
  }
  return weekdayRow ?? plan.days[0];
}

function priceOn(converted, name, hours, date, customer, holidays) {
  const plans = converted.regular.flatMap((table) => table.plans).filter((p) => p.name === name && p.hours === hours);
  const prices = plans.map((p) => deployedPickDayRow(p, date, holidays).prices[customer]).filter((x) => x !== undefined);
  return prices.length ? prices : null;
}

test('状態のない旧形式・料金なしの状態の記録は金額にしない', () => {
  const legacy = doc.shops['js-joysound-kanayama'];
  assert.equal(convertJoysoundShop(legacy, { today: '2027-01-20' }).reason, 'old-record');
  const { status: _status, ...noStatus } = kanayama;
  assert.equal(convertJoysoundShop(noStatus, { today: '2027-01-20' }).reason, 'no-status');
  assert.equal(convertJoysoundShop(v2['js-joysound-akitsu'], { today: '2027-01-20' }).reason, 'contact');
  for (const status of ['season', 'unparsed', 'unverified']) {
    assert.equal(convertJoysoundShop({ ...kanayama, status }, { today: '2027-01-20' }).reason, status);
  }
  assert.equal(convertJoysoundShop(kanayama, {}).reason, 'season-unchecked');
  const { notes: _notes, ...noNotes } = kanayama;
  assert.equal(convertJoysoundShop(noNotes, { today: '2027-01-20' }).reason, 'drink-rule-unchecked');
  assert.ok(convertJoysoundShop(kanayama, { today: '2027-01-20' }).regular.length);
});

test('期間告知は変換する日から 21 日先までと重なれば金額にしない', () => {
  const notice = (from, until) => ({ ...kanayama, notices: [{ from, until, text: '年末年始特別料金' }] });
  // 12/29 から: 12/8 の 21 日先（12/29 の終わりまで）と重なる。12/7 では重ならない
  const yearEnd = notice(jst('2026-12-29T00:00'), jst('2027-01-04T06:00'));
  assert.equal(convertJoysoundShop(yearEnd, { today: '2026-12-08' }).reason, 'season');
  assert.equal(convertJoysoundShop(yearEnd, { today: '2026-12-07' }).reason, undefined);
  assert.equal(convertJoysoundShop(yearEnd, { today: '2027-01-03' }).reason, 'season');
  // 終わりの日の朝 6 時までは、その日を選ぶ人がいるので出さない。翌日から出す
  assert.equal(convertJoysoundShop(yearEnd, { today: '2027-01-04' }).reason, 'season');
  assert.equal(convertJoysoundShop(yearEnd, { today: '2027-01-05' }).reason, undefined);
  // 終わりが読めない告知は出さない
  assert.equal(convertJoysoundShop(notice(jst('2026-12-29T00:00'), null), { today: '2027-06-01' }).reason, 'season');
  assert.equal(convertJoysoundShop(notice(null, null), { today: '2027-06-01' }).reason, 'season');
});

test('平日だけ・土日祝だけのプランに空の行を足し、配信中のアプリでも別の曜日に金額が出ない', () => {
  const converted = convertJoysoundShop(kanayama, { today: '2027-01-20' });
  const saturday = new Date(2027, 0, 23, 12, 0);
  const monday = new Date(2027, 0, 25, 12, 0);
  // 平日の 30 分 11:00〜18:00 は土曜に使わない。土曜は土日祝の 10:00〜18:00 だけ（学生 275 円）
  assert.equal(priceOn(converted, '30分', '11:00～18:00', saturday, '学生'), null);
  assert.deepEqual(priceOn(converted, '30分', '10:00～18:00', saturday, '学生'), [275]);
  assert.equal(priceOn(converted, '30分', '10:00～18:00', monday, '学生'), null);
  // 平日だけの昼フリータイム 858 円を土曜に、土日祝だけの 1298 円を平日に出さない
  assert.equal(priceOn(converted, '昼フリータイム', '11:00～18:00', saturday, '学生'), null);
  assert.equal(priceOn(converted, '昼フリータイム', '10:00～18:00', monday, '学生'), null);
  // 祝日の月曜は週末の料金。夜の平日は金・土・祝前以外（日曜の夜は平日料金）
  const holiday = new Set(['2027-01-11']);
  assert.equal(priceOn(converted, '30分', '11:00～18:00', new Date(2027, 0, 11, 12, 0), '学生', holiday), null);
  assert.deepEqual(priceOn(converted, '30分', '18:00～5:00', new Date(2027, 0, 24, 20, 0), '会員'), [484]);
  assert.deepEqual(priceOn(converted, '30分', '18:00～5:00', new Date(2027, 0, 22, 20, 0), '会員'), [583]);
  // どのプランも 2 行以上（1 行だけのプランは配信中のアプリが全曜日に使う）
  for (const plan of converted.regular.flatMap((table) => table.plans)) assert.ok(plan.days.length >= 2, plan.name);
  for (const plan of convertJoysoundShop(aobadai, { today: '2027-01-20' }).regular.flatMap((table) => table.plans)) {
    assert.ok(plan.days.length >= 2, plan.name);
  }
});

test('例外の付いた平日・読めない区分・重なる区分の表は、別の曜日に金額が出る形では読まない', () => {
  const row = (label, price) => ({ label, prices: { 会員: price }, drinks: { 会員: '' } });
  const section = (period, day, rows) => ({ period, day, rows });
  // 「平日 (東京ドームイベント日は週末料金)」のように読めない区分がある時間帯は、ほかの区分の表も読まない
  const dome = convertJoysound([
    section('昼', '平日 (東京ドームイベント日は週末料金)', [row('30分 12:00〜18:00', 300)]),
    section('昼', '週末 (土・日・祝)', [row('30分 12:00〜18:00', 400)]),
    section('夜', '平日', [row('30分 18:00〜翌5:00', 500)]),
    section('夜', '週末 (金・土 ・祝前)', [row('30分 18:00〜翌5:00', 600)]),
  ]);
  assert.deepEqual(dome.regular[0].plans.map((plan) => plan.hours), ['18:00～5:00']);
  assert.deepEqual(dome.regular[0].plans[0].days, [{ day: '平日', prices: { 会員: 500 } }, { day: '金土祝前', prices: { 会員: 600 } }]);
  // 週末の区分が読めない時間帯は、平日の表も読まない（金曜の夜に平日料金が出てしまう）
  const night = convertJoysound([
    section('夜', '平日', [row('30分 18:00〜翌5:00', 500)]),
    section('夜', '週末 (金・土・祝前) ※イベント日を除く', [row('30分 18:00〜翌5:00', 600)]),
  ]);
  assert.equal(night.reason, 'no-readable-rates');
  // 「月〜木」と「日・祝」のように同じ日（祝日の月曜）に当たり得る区分の時間帯は読まない
  const overlap = convertJoysound([
    section('昼', '月〜木', [row('30分 12:00〜18:00', 300)]),
    section('昼', '金・土', [row('30分 12:00〜18:00', 400)]),
    section('昼', '日・祝', [row('30分 12:00〜18:00', 450)]),
  ]);
  assert.equal(overlap.reason, 'no-readable-rates');
  // 祝日の記載がない時間帯は、祝日の料金が分からないので読まない
  assert.equal(convertJoysound([
    section('昼', '平日', [row('30分 12:00〜18:00', 300)]),
    section('昼', '週末 (土・日)', [row('30分 12:00〜18:00', 400)]),
  ]).reason, 'no-readable-rates');
});

test('受付の条件は最終受付として読み、読めなければそのプランを出さない', () => {
  const free = (label) => convertJoysound([
    { period: '昼', day: '平日', rows: [{ label: '30分 11:00〜18:00', prices: { 会員: 200 }, drinks: { 会員: '' } },
      { label, prices: { 会員: 1500 }, drinks: { 会員: '' } }] },
    { period: '昼', day: '週末 (土・日・祝)', rows: [{ label: '30分 11:00〜18:00', prices: { 会員: 300 }, drinks: { 会員: '' } }] },
  ]).regular.find((table) => table.band === 'フリータイム')?.plans ?? [];
  assert.equal(free('昼 フリータイム (受付14:59迄) 11:00〜20:00')[0].last_entry, '14:59');
  assert.equal(free('フリータイム 11:00〜22:00 ※受付は18時まで')[0].last_entry, '18:00');
  assert.equal(free('夕方 フリータイム 14:00〜22:00 ※最終受付17:59')[0].last_entry, '17:59');
  assert.deepEqual(free('フリータイム 受付9:00〜10:59 11:00〜14:00'), []);
  assert.deepEqual(free('フリータイム 11:00〜20:00 ※受付は混雑状況による'), []);
});

test('料金欄の ※ 注記のワンドリンク制を、ドリンクバー付きでないプランに 1ドリンク別として付ける', () => {
  assert.equal(drinkRule(kanayama.notes['昼']), 'one');
  assert.equal(drinkRule(aobadai.notes['昼']), 'bar');
  assert.equal(drinkRule([]), '');
  assert.equal(drinkRule(['※1オーダー制です。']), 'one');
  assert.equal(drinkRule(['※お一人様1ドリンクのご注文をお願いします。']), 'one');
  // 一部のプランだけの決まりや、読めない書き方は付け方を決められない（その店を出さない）
  assert.equal(drinkRule(['※フリータイムはワンドリンク制です。']), null);
  assert.equal(drinkRule(['※ドリンク代は別途頂戴します。']), null);
  assert.equal(drinkRule(['※全プランワンドリンクオーダー制', '※全プランドリンクバー無料']), null);
  const converted = convertJoysoundShop(kanayama, { today: '2027-01-20' });
  const plans = converted.regular.flatMap((table) => table.plans);
  assert.ok(plans.every((plan) => plan.drink === '1ドリンク別'));
  assert.equal(convertJoysoundShop({ ...kanayama, notes: { 昼: ['※平日はワンドリンク制です。'], 夜: [] } }, { today: '2027-01-20' }).reason,
    'drink-rule-unclear');
  // プラン名にドリンクバー付とあるプランには 1ドリンク別を付けない
  const bar = convertJoysound([
    { period: '昼', day: '平日', rows: [{ label: '30分 11:00〜18:00', prices: { 会員: 200 }, drinks: { 会員: '' } },
      { label: 'フリータイム (ドリンクバー付) 11:00〜20:00', prices: { 会員: 1500 }, drinks: { 会員: '' } }] },
    { period: '昼', day: '週末 (土・日・祝)', rows: [{ label: '30分 11:00〜18:00', prices: { 会員: 300 }, drinks: { 会員: '' } }] },
  ], '', { notes: { 昼: ['※全プランワンドリンクオーダー制となります。（ドリンクバー付きプランは除く）'] } });
  assert.equal(bar.regular.find((table) => table.band === '30分').plans[0].drink, '1ドリンク別');
  assert.equal(bar.regular.find((table) => table.band === 'フリータイム').plans[0].drink, 'ドリンクバー付');
});
