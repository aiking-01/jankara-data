// ビッグエコーの変換（scripts/web/bigecho.js）。tests/fixtures の固定の見本だけを読む。今日の日付は必ず引数で渡す
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { convertBigEcho, dayOf, specialPeriod, WINDOW_DAYS } from '../scripts/web/bigecho.js';

const { shops } = JSON.parse(readFileSync(new URL('./fixtures/bigecho_shops.json', import.meta.url), 'utf8'));
const convert = (id, today) => {
  const shop = shops[id];
  return convertBigEcho(shop.sections, shop.hours, { notice: shop.notice, priceNotes: shop.price_notes, today });
};
const plansOf = (result, kind, band) => (result[kind] ?? []).filter((table) => table.band === band).flatMap((table) => table.plans);
const noticeBlocks = (text, today) => Boolean(specialPeriod({ notice: text }, dayOf(today)));

// ---- 料金が変わる期間の告知 ----

test('アプリが配信データを使う日数と同じ 21 日先までを見る', () => {
  assert.equal(WINDOW_DAYS, 21);
});

test('店舗からのお知らせの料金改定（be-1343）: 始まりが 21 日先までに入ったら出さない', () => {
  assert.equal(convert('be-1343', '2026-11-26').reason, 'special-period');
  assert.equal(convert('be-1343', '2026-11-10').reason, 'special-period'); // 12/1 は 21 日先
  const before = convert('be-1343', '2026-11-09');
  assert.equal(before.reason, undefined);
  assert.ok(plansOf(before, 'regular', '30分').length);
});

test('料金欄に表がなく特別料金の見出しだけの店（be-0473, be-0744）', () => {
  assert.equal(convert('be-0473', '2026-11-26').reason, 'special-period');
  assert.equal(convert('be-0744', '2026-12-28').reason, 'special-period');
  // 期間が終われば、表がないので料金なし（前回の金額は元データに残っていない）
  assert.equal(convert('be-0473', '2027-01-06').reason, 'no-slot-table');
  assert.equal(convert('be-0744', '2027-01-06').reason, 'no-slot-table');
});

test('料金欄が空で、お知らせだけに年末の特別料金がある店（be-1135, be-1198）', () => {
  assert.deepEqual(shops['be-1135'].sections, []);
  assert.equal(convert('be-1135', '2026-11-26').reason, 'special-period');
  assert.equal(convert('be-1198', '2027-01-03').reason, 'special-period');
  assert.equal(convert('be-1135', '2027-01-04').reason, 'no-slot-table');
});

test('表の注記の特別料金（be-0981）: 12/1～12/31 は 11/26 と 12/15 に止め、1/2 には通す', () => {
  assert.equal(convert('be-0981', '2026-11-26').reason, 'special-period');
  assert.equal(convert('be-0981', '2026-12-15').reason, 'special-period');
  assert.equal(convert('be-0981', '2027-01-02').reason, undefined);
  // 8/1～8/31 の注記もあるので、夏の前にも止める
  assert.equal(convert('be-0981', '2027-07-20').reason, 'special-period');
});

test('店名を変えた店（be-1323）: 年をまたぐ特別料金期間と「12/11以降の料金はお問い合わせ」', () => {
  assert.equal(convert('be-1323-new', '2026-11-26').reason, 'special-period');
  assert.equal(convert('be-1323-new', '2027-01-11').reason, 'special-period');
});

test('学生の期間限定料金が終わりの日付なしで始まった店（be-1149）は出さない', () => {
  assert.equal(convert('be-1149', '2026-11-26').reason, 'special-period');
});

test('夏季料金の表なのに期間の日付がない店（be-0573）は、いつの料金か分からないので出さない', () => {
  assert.equal(convert('be-0573', '2026-11-26').reason, 'special-period');
});

test('特別料金でない表は止めない: 学生特別料金（be-0489）、期間限定プラン（be-0950）、夏だけの注記（be-1418）', () => {
  assert.equal(convert('be-0489', '2026-11-26').reason, undefined);
  assert.equal(convert('be-0950', '2026-11-26').reason, undefined);
  assert.equal(convert('be-1418', '2026-11-26').reason, undefined);
  // be-1418 の「7/21~8/31の期間18時までの料金を週末料金に変更」は、その期間の前には止める
  assert.equal(convert('be-1418', '2027-07-10').reason, 'special-period');
});

test('表の列見出しにある期間（be-0862 の「7/21～9/4 全日」）も読む', () => {
  assert.equal(convert('be-0862', '2026-11-26').reason, undefined);
  assert.equal(convert('be-0862', '2027-07-20').reason, 'special-period');
});

test('年をまたぐ期間の年を、今日に一番近い回として決める', () => {
  for (const day of ['2026-11-26', '2026-12-28', '2027-01-03', '2027-01-05']) assert.ok(noticeBlocks('11/27～1/5は特別料金となります', day), day);
  assert.equal(noticeBlocks('11/27～1/5は特別料金となります', '2027-01-06'), false);
  for (const day of ['2026-12-05', '2026-12-28', '2027-01-03']) assert.ok(noticeBlocks('12/26～1/4は特別料金となります', day), day);
  assert.equal(noticeBlocks('12/26～1/4は特別料金となります', '2026-12-04'), false);
  assert.equal(noticeBlocks('12/26～1/4は特別料金となります', '2027-01-06'), false);
  assert.ok(noticeBlocks('・11月27日(金)～2027年1月11日(月祝)は特別料金期間となります。', '2027-01-11'));
  assert.equal(noticeBlocks('・11月27日(金)～2027年1月11日(月祝)は特別料金期間となります。', '2027-01-12'), false);
});

test('「2026/12/01」を月日と取り違えない', () => {
  assert.ok(noticeBlocks('2026/12/01から料金改定', '2026-11-10'));
  assert.equal(noticeBlocks('2026/12/01から料金改定', '2026-11-09'), false);
  assert.equal(noticeBlocks('2026/11/20（金）より、店名を「A店」から「B店」へ変更することとなりました。', '2026-11-26'), false);
});

test('語を広く読む: 料金が異なる、週末料金に変更、期間限定料金、UP、値上げ、お問い合わせ', () => {
  assert.ok(noticeBlocks('2026年12月18日~1月3日期間中は通常料金とは異なります', '2026-11-27'));
  assert.ok(noticeBlocks('12/21~1/3の期間18時までの料金を週末料金に変更', '2026-12-01'));
  assert.ok(noticeBlocks('12月1日より期間限定料金に変更してます', '2026-11-26'));
  assert.ok(noticeBlocks('12/1～12/31は各コース30分あたり50円(税別)UP', '2026-11-26'));
  assert.ok(noticeBlocks('12月10日より値上げいたします', '2026-11-26'));
  assert.ok(noticeBlocks('※12/11(金)以降の料金は店舗までお問い合わせください', '2026-11-26'));
  assert.ok(noticeBlocks('12月15日から料金が変わります', '2026-11-26'));
  assert.ok(noticeBlocks('12/29～1/3は深夜料金を割増とさせていただきます', '2026-12-20'));
});

test('日付と料金の語が別の文にあっても、同じ段落なら読む', () => {
  const text = '【年末年始の営業について】\n12/29(火)～1/3(日)\n上記期間は特別料金となります';
  assert.ok(noticeBlocks(text, '2026-12-08'));
  assert.equal(noticeBlocks(text, '2026-12-07'), false);
  assert.ok(noticeBlocks('年末年始料金\n12/29・30・31', '2026-12-31'));
  assert.equal(noticeBlocks('年末年始料金\n12/29・30・31', '2027-01-01'), false);
});

test('日付のない一般的な断り書きと、料金と関係のない日付では止めない', () => {
  assert.equal(noticeBlocks('GW・お盆・年末年始などの特別期間は料金が異なる場合がございます', '2026-12-28'), false);
  // be-1110 のお知らせ: チケットの配布日と、日付のないプランの変更
  const tickets = '※８月５日より限定配布しました「夏の思い出チケット」\n９月１日から利用開始！【有効期限2026年10月31日まで】\n※１０００円フリータイム➡よるカラに変更になりました。';
  assert.equal(noticeBlocks(tickets, '2026-11-26'), false);
  // 誕生日の範囲は料金の期間ではない
  assert.equal(noticeBlocks('※18歳の方【平成20年(2008年)4月2日～平成21年(2009年)4月1日生まれ】は、22時までのご利用となります。料金は店舗へお問い合わせください', '2026-11-26'), false);
});

test('料金の告知の語を alerts に入れる（日付のない断り書きでも、警告は出す）', () => {
  const shop = shops['be-1110'];
  const result = convertBigEcho(shop.sections, shop.hours, { notice: '年末年始は料金が異なる場合がございます', today: '2026-11-26' });
  assert.equal(result.reason, undefined);
  assert.ok(result.alerts.includes('年末年始'));
});

// ---- 公式の誤記と最終受付（be-1110） ----

test('桁が 1 つ違う誤記（シニア 154 円）の表は読まない', () => {
  const result = convert('be-1110', '2026-11-26');
  assert.equal(result.reason, undefined);
  const free = plansOf(result, 'regular', 'フリータイム');
  assert.equal(free.find((plan) => plan.name === '昼フリータイム'), undefined);
  for (const plan of free) for (const row of plan.days) assert.ok(Object.values(row.prices).every((value) => value >= 500), plan.name);
});

test('「受付時間15:00から18:00受付終了」の最終受付は 18:00', () => {
  const evening = plansOf(convert('be-1110', '2026-11-26'), 'regular', 'フリータイム').find((plan) => plan.name === '夕方フリータイム');
  assert.equal(evening.last_entry, '18:00');
});

test('同じ最終受付が 2 か所に書いてある表は読む（be-0563 の 3 時間パック）', () => {
  const pack = plansOf(convert('be-0563', '2026-11-26'), 'regular', 'フリータイム').find((plan) => plan.name === '3時間パック');
  assert.equal(pack.last_entry, '17:00');
});

test('受付の文に違う時刻が 2 つあって範囲として読めない表は読まない', () => {
  const shop = shops['be-1110'];
  const section = shop.sections.find((s) => /夕方フリータイム/.test(s.time));
  const odd = { ...section, title: '受付15:00 18:00受付終了' };
  const result = convertBigEcho([...shop.sections.filter((s) => s !== section), odd], shop.hours, { today: '2026-11-26' });
  assert.equal(plansOf(result, 'regular', 'フリータイム').find((plan) => plan.name === '夕方フリータイム'), undefined);
});

// ---- 30 分表の注記にだけある 1 人の追加料金 ----

test('「30分毎に+50円」は会員の金額に足す（be-0141）', () => {
  const result = convert('be-0141', '2026-11-26');
  const regular = plansOf(result, 'regular', '30分');
  const solo = plansOf(result, 'solo', '30分');
  assert.equal(solo.length, regular.length);
  for (const plan of solo) {
    const base = regular.find((other) => other.hours === plan.hours);
    for (const row of plan.days.filter((day) => day.prices.会員 !== undefined)) {
      assert.equal(row.prices.会員, base.days.find((day) => day.day === row.day).prices.会員 + 50);
    }
  }
});

test('読めない形の 1 人の追加料金（入室時に+200円、曜日で違う）の店は出さない', () => {
  assert.equal(convert('be-0600', '2026-11-26').reason, 'solo-note');
  assert.equal(convert('be-1436', '2026-11-26').reason, 'solo-note');
});

test('「デュアルプロジェクターのご利用は、お一人様30分+110円」は 1 人利用の追加料金ではない（be-1418）', () => {
  const result = convert('be-1418', '2026-11-26');
  assert.equal(result.reason, undefined);
  for (const plan of plansOf(result, 'solo', '30分')) {
    for (const row of plan.days) assert.ok(Object.values(row.prices).every((value) => value < 1000));
  }
});

test('同じ時間帯の 1 人の料金は 1 つだけにし、1 ドリンクの表を基準にする（be-1168）', () => {
  const result = convert('be-1168', '2026-11-26');
  const solo = plansOf(result, 'solo', '30分');
  assert.equal(new Set(solo.map((plan) => plan.hours)).size, solo.length);
  const regular = plansOf(result, 'regular', '30分');
  for (const plan of solo) {
    const base = regular.find((other) => other.hours === plan.hours && other.drink === '1ドリンク別');
    assert.ok(base, plan.hours);
    const weekday = plan.days.find((day) => day.day === '平日');
    assert.equal(weekday.prices.会員, base.days.find((day) => day.day === '平日').prices.会員 + 50);
  }
});

// ---- 税別 ----

test('ほかの表の注記や表のない見出しに税抜とある店は、税込の併記がない表を読まない', () => {
  const shop = shops['be-0141'];
  const notes = convertBigEcho(shop.sections, shop.hours, { priceNotes: ['※表示価格は税抜です'], today: '2026-11-26' });
  assert.equal(notes.reason, 'tax-excluded');
  const sections = shop.sections.map((section, index) => (index === shop.sections.length - 1 ? { ...section, note: `${section.note} ※表示価格は全て税抜です` } : section));
  assert.equal(convertBigEcho(sections, shop.hours, { today: '2026-11-26' }).reason, 'tax-excluded');
});

test('フリータイムの表だけに税別とある店は、そのフリータイムを読まない', () => {
  const shop = shops['be-1110'];
  const sections = shop.sections.map((section) => (/夕方フリータイム/.test(section.time) ? { ...section, note: `${section.note} ※料金は税別です` } : section));
  const result = convertBigEcho(sections, shop.hours, { today: '2026-11-26' });
  // 店のどこかに税別とあれば、30 分料金も含めて税込の併記がない表は読まない
  assert.equal(result.reason, 'tax-excluded');
});

test('今日を省略しても変換できる（日本時間の今日を使う）', () => {
  const shop = shops['be-0489'];
  const result = convertBigEcho(shop.sections, shop.hours);
  assert.ok(result.reason === undefined || result.reason === 'special-period');
});
