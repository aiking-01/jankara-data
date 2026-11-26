// 歌広場の変換器（scripts/web/utahiro.js）。tests/fixtures/utahiro_prices.json は 2026-11-26 に保存した店のページを
// scripts/update_utahiro_prices.py で読んだもの（料金表・営業時間・告知の文字だけ）
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import {
  convertUtahiro, convertUtahiroShops, dayOf, noticePeriods, overlapsHorizon, parseUtahiroHours, todayJst,
} from '../scripts/web/utahiro.js';

const fixture = JSON.parse(readFileSync(new URL('./fixtures/utahiro_prices.json', import.meta.url), 'utf8'));
const shops = fixture.shops;
const copy = (value) => JSON.parse(JSON.stringify(value));
const AFTER_NOTICES = '2027-01-15'; // 年末年始の料金変更（～1/8）が終わった後
const planOf = (converted, band, hours) => converted.regular.find((table) => table.band === band)
  .plans.find((plan) => plan.hours === hours);

test('今の告知（12/1 または 12/4 から 1/8 まで、一部料金を変更）の間は、どの店も出さない', () => {
  const { shops: converted, reasons } = convertUtahiroShops(shops, '2026-11-26');
  assert.deepEqual(converted, {});
  assert.deepEqual(reasons['notice-period'].sort(), Object.keys(shops).sort());
});

test('告知の期間が終われば、読める形の店だけを出す', () => {
  const { shops: converted, reasons } = convertUtahiroShops(shops, AFTER_NOTICES);
  assert.deepEqual(Object.keys(converted).sort(), ['uh-1', 'uh-113', 'uh-139', 'uh-143', 'uh-205', 'uh-94']);
  assert.deepEqual(reasons['24h-open-close'], ['uh-130']); // 24時間営業なのに OPEN~18:00 / 18:00~CLOSE
  assert.deepEqual(reasons['notice-period'], ['uh-133']); // 「12月18日から営業時間を変更」は終わりがない
});

test('川崎第一京浜通り店: 公式の表のとおりの 30 分とフリータイム、営業時間', () => {
  const converted = convertUtahiro(shops['uh-1']);
  assert.deepEqual(converted.regular, [
    { band: '30分', customers: ['学生会員', '一般'], note: '', plans: [
      { name: '30分', hours: '開店～18:00', last_entry: '', drink: 'ドリンクバー付', days: [
        { day: '平日', prices: { 学生会員: 160, 一般: 200 } }, { day: '土日祝', prices: { 学生会員: 240, 一般: 300 } }] },
      { name: '30分', hours: '18:00～閉店', last_entry: '', drink: 'ドリンクバー付', days: [
        { day: '平日', prices: { 学生会員: 350, 一般: 480 } }, { day: '金土祝前', prices: { 学生会員: 400, 一般: 530 } }] },
    ] },
    { band: 'フリータイム', customers: ['学生会員', '一般'], note: '', plans: [
      { name: 'フリータイム', hours: '11:00～20:00', last_entry: '', drink: 'ドリンクバー付', days: [
        { day: '平日', prices: { 学生会員: 740, 一般: 1250 } }, { day: '土日祝', prices: { 学生会員: 1500, 一般: 2000 } }] },
      { name: 'フリータイム', hours: '18:00～5:00', last_entry: '', drink: 'ドリンクバー付', days: [
        { day: '平日', prices: { 一般: 1600 } }, { day: '金土祝前', prices: { 一般: 2500 } }] },
    ] },
  ]);
  const week = Object.fromEntries(['月', '火', '水', '木', '金', '土', '日', '祝前', '日祝'].map((key) => [key, [660, 1740]]));
  assert.deepEqual(converted.week, week);
  assert.equal(converted.solo, undefined); // 1 人の料金は表にない（engine が「記載がない」と添える）
});

test('「※2名様より受付」の列は出さない（1 人にフリータイムの金額を出さない）', () => {
  for (const id of ['uh-113', 'uh-139']) {
    const plan = planOf(convertUtahiro(shops[id]), 'フリータイム', '11:00～20:00');
    assert.deepEqual(plan.days[1], { day: '土日祝', prices: {} }, id);
    assert.ok(Object.keys(plan.days[0].prices).length, id);
  }
});

test('「－」の曜日はそのプランがない', () => {
  const plan = planOf(convertUtahiro(shops['uh-113']), 'フリータイム', '22:00～5:00');
  assert.deepEqual(plan.days, [{ day: '平日', prices: { 一般: 2000 } }, { day: '金土祝前', prices: {} }]);
});

test('曜日で時刻が違うフリータイムは、曜日ごとのプランに分ける', () => {
  const converted = convertUtahiro(shops['uh-143']); // 「11:00~20:00 / 土日祝10:00～20:00」
  assert.deepEqual(planOf(converted, 'フリータイム', '11:00～20:00').days,
    [{ day: '平日', prices: { 学生会員: 1200, 一般: 1600 } }, { day: '土日祝', prices: {} }]);
  assert.deepEqual(planOf(converted, 'フリータイム', '10:00～20:00').days,
    [{ day: '平日', prices: {} }, { day: '土日祝', prices: { 学生会員: 1700, 一般: 2300 } }]);
  // 「(金・土・祝前日 18:00～翌5:00)」の形
  const record = copy(shops['uh-1']);
  const night = record.bands[1].courses[0].tables[1];
  night.title = 'お一人様フリータイム料金\n18:00～22:00\n(金・土・祝前日 18:00～翌5:00)';
  const split = convertUtahiro(record);
  assert.deepEqual(planOf(split, 'フリータイム', '18:00～22:00').days,
    [{ day: '平日', prices: { 一般: 1600 } }, { day: '金土祝前', prices: {} }]);
  assert.deepEqual(planOf(split, 'フリータイム', '18:00～5:00').days,
    [{ day: '平日', prices: {} }, { day: '金土祝前', prices: { 一般: 2500 } }]);
});

test('24時間営業で時刻を書いた表は、全曜日 0:00～24:00 の営業時間と時刻の帯にする', () => {
  const converted = convertUtahiro(shops['uh-94']);
  assert.deepEqual(converted.regular[0].plans.map((plan) => plan.hours), ['8:00～18:00', '18:00～8:00']);
  assert.ok(Object.values(converted.week).every(([open, close]) => open === 0 && close === 1440));
});

test('営業時間は <br> の後の曜日別の行まで読む', () => {
  assert.equal(shops['uh-205'].hours, '11:00～翌5:00\n金・土・祝前日：11:00～翌8:00');
  const week = parseUtahiroHours(shops['uh-205'].hours).week;
  assert.deepEqual([week.木, week.金, week.土, week.祝前, week.日, week.日祝],
    [[660, 1740], [660, 1920], [660, 1920], [660, 1920], [660, 1740], [660, 1740]]);
  const sunday = parseUtahiroHours(shops['uh-139'].hours).week; // 「11:00～翌5:00 / 土・日・祝日：10:00～翌5:00」
  assert.deepEqual([sunday.金, sunday.土, sunday.日, sunday.日祝, sunday.祝前], [[660, 1740], [600, 1740], [600, 1740], [600, 1740], [660, 1740]]);
});

test('曜日の規則が 2 つ当たる日（祝前日の日曜・土曜、祝日の金曜など）は、狭い方の営業時間にする', () => {
  // 綱島店「午前11:00～翌5:00 (金祝前11：00～翌6：00/土：午前10:00～翌6:00/日祝：午前10:00～翌5:00)」
  const { week } = parseUtahiroHours(shops['uh-143'].hours);
  assert.deepEqual(week, {
    日: [600, 1740], 月: [660, 1740], 火: [660, 1740], 水: [660, 1740], 木: [660, 1740],
    金: [660, 1800], 土: [600, 1800], 祝前: [660, 1740], 日祝: [660, 1740],
  });
});

test('知らない形の営業時間は読まない', () => {
  for (const text of ['', '11時～翌5時', '月～木 11:00～翌5:00', '午後1:00～翌5:00', '11:00～翌5:00\n平日：11:00～23:00',
    '11:00～翌5:00\n金・土・祝前日：11:00～', '24時間', '11:00～23:00\n金・土・祝前日：23:00～23:00']) {
    assert.equal(parseUtahiroHours(text), null, text);
  }
  assert.ok(parseUtahiroHours('24時間営業').always);
});

test('告知は年月日と曜日のある決まった形だけを読む。～ と 〜 のどちらでもよい', () => {
  const price = '2026年12月4日(金)～2027年1月8日(金)まで、一部料金を変更させていただきます。詳しくは店舗まで直接お問い合わせをお願いいたします。';
  assert.deepEqual(noticePeriods([price]), [{ from: dayOf('2026-12-04'), to: dayOf('2027-01-08'), price: true }]);
  assert.deepEqual(noticePeriods([price.replace('～', '〜')]), noticePeriods([price]));
  assert.deepEqual(noticePeriods([`・${price.replace('(金)', '（金）')}`]), noticePeriods([price]));
  assert.deepEqual(noticePeriods(['・2026年12月19日(土)～2027年1月3日(日)の期間中はオープン時間を10時へ変更いたします。']),
    [{ from: dayOf('2026-12-19'), to: dayOf('2027-01-03'), price: false }]);
  assert.deepEqual(noticePeriods(['・2026年12月18日(金)から営業時間を11時～翌5時へ変更いたします。']),
    [{ from: dayOf('2026-12-18'), to: Infinity, price: false }]);
  assert.deepEqual(noticePeriods([]), []);
  for (const text of [
    '12月4日(金)～1月8日(金)まで、一部料金を変更させていただきます。', // 年がない
    '2026年12月4日(木)～2027年1月8日(金)まで、一部料金を変更させていただきます。', // 曜日が日付と合わない
    '2027年1月8日(金)～2026年12月4日(金)まで、一部料金を変更させていただきます。', // 終わりが先
    '2026年12月24日(木)は貸切営業となります。',
    '年末年始は特別料金となります。',
  ]) {
    assert.equal(noticePeriods([text]), null, text);
  }
});

test('告知の期間は、変換する日から 21 日先までの滞在（前の日の夜と最後の日の翌朝を含む）と重なれば出さない', () => {
  const today = dayOf('2026-11-26');
  const period = (from, to) => ({ from: dayOf(from), to: dayOf(to) });
  assert.equal(overlapsHorizon(period('2026-12-18', '2027-01-08'), today), true); // 21 日後の夜から翌朝にかかる
  assert.equal(overlapsHorizon(period('2026-12-19', '2027-01-08'), today), false);
  assert.equal(overlapsHorizon(period('2026-11-01', '2026-11-25'), today), true); // 昨夜からの滞在
  assert.equal(overlapsHorizon(period('2026-11-01', '2026-11-24'), today), false);
  assert.equal(overlapsHorizon({ from: dayOf('2026-12-18'), to: Infinity }, today), true);
  // 店の変換でも同じ。12/19 からの料金変更は 11/26 には出す、11/27 には出さない
  const record = copy(shops['uh-1']);
  record.notices = ['2026年12月19日(土)～2027年1月8日(金)まで、一部料金を変更させていただきます。'];
  assert.deepEqual(Object.keys(convertUtahiroShops({ 'uh-1': record }, '2026-11-26').shops), ['uh-1']);
  assert.deepEqual(convertUtahiroShops({ 'uh-1': record }, '2026-11-27').reasons, { 'notice-period': ['uh-1'] });
});

test('読めない告知のある店は出さない', () => {
  const record = copy(shops['uh-1']);
  record.notices = ['2027年2月1日(月)より一部料金を改定いたします。'];
  assert.deepEqual(convertUtahiroShops({ 'uh-1': record }, AFTER_NOTICES).reasons, { 'notice-unknown': ['uh-1'] });
});

test('ほかの店で見つかった料金変更は、その告知を見たより前に確かめた店にも当てる', () => {
  const notice = '2027年1月20日(水)～2027年1月31日(日)まで、一部料金を変更させていただきます。';
  const seen = { ...copy(shops['uh-205']), notices: [notice], checked_at: 2000 };
  const older = { ...copy(shops['uh-1']), notices: [], checked_at: 1000 };
  const newer = { ...copy(shops['uh-94']), notices: [], checked_at: 3000 };
  const result = convertUtahiroShops({ 'uh-205': seen, 'uh-1': older, 'uh-94': newer }, AFTER_NOTICES);
  assert.deepEqual(result.reasons, { 'notice-period': ['uh-205'], 'notice-other-shop': ['uh-1'] });
  assert.deepEqual(Object.keys(result.shops), ['uh-94']);
  // 営業時間の変更は店ごとのことなので、ほかの店には当てない
  seen.notices = ['2027年1月20日(水)～2027年1月31日(日)の期間中はオープン時間を10時へ変更いたします。'];
  assert.deepEqual(Object.keys(convertUtahiroShops({ 'uh-205': seen, 'uh-1': older }, AFTER_NOTICES).shops), ['uh-1']);
});

test('知らない形が 1 つでもあれば店ごと出さない', () => {
  const cases = [
    ['extras', (r) => { r.extras = ['orphan_row: ["一般","500","600"]']; }],
    ['extras', (r) => { r.extras = ['hidden: ible-block db-menu-tab']; }],
    ['tax', (r) => { r.tail = ['ソフトドリンク飲み放題 ドリンクバーをご利用いただけます。']; }],
    ['extra-charge', (r) => { r.tail = ['お1人様税込料金です。\n別途ルームチャージがかかります。']; }],
    ['hours', (r) => { r.hours = '11時～翌5時'; }],
    ['band-count', (r) => { r.bands.splice(1, 1); }],
    ['unknown-band', (r) => { r.bands[2].label = '深夜パック'; }],
    ['band-not-open-close', (r) => { r.bands[0].label = '11:00~18:00'; }],
    ['band-outside-hours', (r) => { r.hours = '19:00～翌5:00'; }],
    ['unknown-course', (r) => { r.bands[0].courses[1].name = 'プレミアム飲み放題'; }],
    ['day-header', (r) => { r.bands[0].courses[0].tables[0].header[2] = '土・日'; }],
    ['day-header', (r) => { r.bands[0].courses[0].tables[0].header[2] = '土・日・祝日\n※学生証の提示が必要'; }],
    ['slot-group-only', (r) => { r.bands[0].courses[0].tables[0].header[2] = '土・日・祝日\n※2名様より受付'; }],
    ['slot-rows', (r) => { r.bands[0].courses[0].tables[0].rows[0][1] = '160円～'; }],
    ['slot-rows', (r) => { r.bands[0].courses[0].tables[0].rows[0][0] = 'シニア'; }],
    ['slot-rows', (r) => { r.bands[0].courses[0].tables[0].rows.shift(); }], // 学生会員の行がない 30 分の表
    ['slot-dash', (r) => { r.bands[0].courses[0].tables[0].rows.forEach((row) => { row[2] = '－'; }); }],
    ['slot-count', (r) => { r.bands[0].courses[0].tables.push(copy(r.bands[0].courses[0].tables[0])); }],
    ['free-title', (r) => { r.bands[0].courses[0].tables[1].title = 'お一人様フリータイム料金\n11:00~20:00\n土・日・祝日は3時間パック'; }],
    ['free-rows', (r) => { r.bands[0].courses[0].tables[1].rows[0][2] = '－'; }], // 学生会員だけ「－」
    ['free-overlap', (r) => { r.bands[0].courses[0].tables.push(copy(r.bands[0].courses[0].tables[1])); }],
    ['free-outside-hours', (r) => { r.hours = '11:00～翌3:00'; }], // 夜のフリータイムが翌5:00 まで
  ];
  for (const [reason, change] of cases) {
    const record = copy(shops['uh-1']);
    change(record);
    assert.deepEqual(convertUtahiro(record), { reason }, `${reason}: ${change}`);
  }
  assert.deepEqual(convertUtahiro({ ...copy(shops['uh-1']), bands: [], missing: 'HTTP 404' }), { reason: 'missing' });
  assert.deepEqual(convertUtahiro({ ...copy(shops['uh-1']), bands: [] }), { reason: 'no-table' });
  assert.deepEqual(convertUtahiro(shops['uh-133']), { reason: 'free-title' }); // 土・日・祝日は3時間パック
});

test('列見出しの曜日は、engine の読み方（「平日」はもう一方の列に当たらない日）で公式の定義と一致する', () => {
  const WEEK = ['日', '月', '火', '水', '木', '金', '土'];
  const official = {
    '月~金(祝日を除く)': (c) => c.w >= 1 && c.w <= 5 && !c.holiday,
    '土・日・祝日': (c) => c.w === 0 || c.w === 6 || c.holiday,
    '日~木(祝前日を除く)': (c) => c.w <= 4 && !c.pre,
    '金・土・祝前日': (c) => c.w === 5 || c.w === 6 || c.pre,
    '月~木(祝日・祝前日を除く)': (c) => c.w >= 1 && c.w <= 4 && !c.holiday && !c.pre,
    '金・土・日・祝日・祝前日': (c) => c.w === 0 || c.w >= 5 || c.holiday || c.pre,
  };
  // engine.js の labelMatches と pickDayRow と同じ読み方
  const hits = (label, c) => label.match(/祝前|[月火水木金土日祝]/g)
    .some((t) => (t === '祝' ? c.holiday : t === '祝前' ? c.pre : WEEK[c.w] === t));
  const engineRow = (days, c) => days.find((row) => row.day !== '平日' && hits(row.day, c)) ?? days.find((row) => row.day === '平日');
  const pairs = [['月~金(祝日を除く)', '土・日・祝日'], ['日~木(祝前日を除く)', '金・土・祝前日'],
    ['月~木(祝日・祝前日を除く)', '金・土・日・祝日・祝前日']];
  for (const [first, second] of pairs) {
    const record = copy(shops['uh-1']);
    record.bands[0].courses[0].tables[0].header = ['', first, second];
    const { days } = convertUtahiro(record).regular[0].plans[0];
    for (let w = 0; w < 7; w += 1) {
      for (const holiday of [false, true]) {
        for (const pre of [false, true]) {
          const c = { w, holiday, pre };
          assert.equal(engineRow(days, c), official[first](c) ? days[0] : days[1], `${first}/${second} ${JSON.stringify(c)}`);
          assert.notEqual(official[first](c), official[second](c));
        }
      }
    }
  }
});

test('1 店の記録が壊れていても、ほかの店の変換を止めない', () => {
  const result = convertUtahiroShops({ 'uh-1': shops['uh-1'], 'uh-x': null, 'uh-y': { notices: 5 } }, AFTER_NOTICES);
  assert.deepEqual(Object.keys(result.shops), ['uh-1']);
  assert.deepEqual(result.reasons, { record: ['uh-x'], 'notice-unknown': ['uh-y'] });
  assert.throws(() => convertUtahiroShops({}, '2026/11/26'), /変換する日/);
});

test('変換する日は JST。CONVERT_TODAY で差し替えられる', () => {
  assert.equal(todayJst({}, Date.UTC(2026, 10, 25, 15, 0)), '2026-11-26'); // UTC 15:00 は JST 0:00
  assert.equal(todayJst({}, Date.UTC(2026, 10, 25, 14, 59)), '2026-11-25');
  assert.equal(todayJst({ CONVERT_TODAY: '2027-01-15' }), '2027-01-15');
});
