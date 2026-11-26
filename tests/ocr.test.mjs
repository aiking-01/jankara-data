// OCR が書き出した HTML から金額を作る処理を確かめる
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import { convertKanOcr, convertKanShop, STALE_DAYS, todayJst, withoutPeriods } from '../scripts/web/ocr.js';

const head = (range, drink, customers) => `<thead><tr><th rowspan="2">${range}</th><th colspan="3">${drink}</th></tr>`
  + `<tr>${customers.map((name) => `<th>${name}</th>`).join('')}</tr></thead>`;
const body = (rows) => `<tbody>${rows.map(([day, ...prices]) => `<tr><td>${day}</td>${prices.map((price) => `<td>${price}</td>`).join('')}</tr>`).join('')}</tbody>`;
const table = (range, drink, customers, rows) => `<div data-label="Table"><table>${head(range, drink, customers)}${body(rows)}</table></div>`;
const page = (...parts) => ['<div data-label="Section-Header"><h1>～お1人様30分 税込料金～</h1></div>', ...parts].join('\n');

const day = table('9:00~19:00', 'ドリンクバー付', ['学生', '会員', '一般'], [['月~金', '¥150', '¥170', '¥226'], ['土・日・祝', '¥230', '¥280', '¥373']]);
const night = table('19:00~6:00', '1ドリンク制', ['学生', '会員', '一般'], [['月~木', '¥275', '¥380', '¥506'], ['金・土・日<br>祝・祝前日', '¥350', '¥480', '¥640']]);
const solo = '<div data-label="Text"><p>★土・日・祝(OPEN~19:00)の、お一人様でのご利用は<br>上記金額+ ¥50 となります</p></div>';

test('カラオケ館: 2 回の読み取りが同じなら時間帯ごとの金額を返す', () => {
  const html = page(day, night);
  const result = convertKanOcr([html, html]);
  assert.equal(result.reason, undefined);
  const [first, second] = result.regular[0].plans;
  assert.equal(first.hours, '9:00～19:00');
  assert.deepEqual(first.days[0].prices, { 学生会員: 150, 学生: 150, 会員: 170, 一般: 226 });
  assert.equal(first.drink, 'ドリンク条件は公式の表で確認');
  assert.equal(second.hours, '19:00～6:00');
  assert.equal(second.days[1].prices.一般, 640);
  assert.deepEqual(result.solo, []);
});

test('カラオケ館: 読み取りが 1 回だけか、2 回で金額が違えば使わない', () => {
  const html = page(day, night);
  assert.equal(convertKanOcr([html]).reason, 'single-read');
  assert.equal(convertKanOcr([html, html.replace('¥150', '¥160')]).reason, 'reads-differ');
  // 一般が会員の 4/3 倍（切り捨て）と合わない行は、2 回とも同じでも読み違い
  assert.equal(convertKanOcr([html, html.replace('¥226', '¥228')]).reason, 'ratio');
  const both = html.replace('¥226', '¥228');
  assert.equal(convertKanOcr([both, both]).reason, 'ratio');
});

test('カラオケ館: 1 人の上乗せは注記の曜日と時間帯の表にだけ足す', () => {
  const html = page(day, solo, night);
  const result = convertKanOcr([html, html]);
  const [first, second] = result.solo[0].plans;
  assert.deepEqual(first.days[0].prices, { 学生会員: 150, 学生: 150, 会員: 170, 一般: 226 });
  assert.deepEqual(first.days[1].prices, { 学生会員: 280, 学生: 280, 会員: 330, 一般: 423 });
  assert.deepEqual(second.days, result.regular[0].plans[1].days);
});

test('カラオケ館: 上乗せの時間帯が表の区切りと合わない店や、時間帯のない注記は読まない', () => {
  const half = solo.replace('OPEN~19:00', '12:00~19:00');
  assert.equal(convertKanOcr([page(day, half, night), page(day, half, night)]).reason, 'solo-window');
  const always = '<p>★ひとりカラオケのご利用は上記金額+¥50となります</p>';
  assert.equal(convertKanOcr([page(day, always, night), page(day, always, night)]).reason, 'solo-window');
  const other = '<p>(学生は上記料金+¥200となります</p>';
  assert.equal(convertKanOcr([page(day, other, night), page(day, other, night)]).reason, 'surcharge-note');
});

test('カラオケ館: ドリンクバーの注文が必須の時間帯は出さない', () => {
  const morning = table('9:00~11:00', '会員・一般のお客様は別途ドリンクバー¥660をご注文いただきます。', ['学生', '会員', '一般'],
    [['月~金', '¥180', '¥150', '¥200'], ['土・日・祝', '¥240', '¥320', '¥426']]);
  const html = page(morning, night.replace('19:00~6:00', '11:00~6:00'));
  const plans = convertKanOcr([html, html]).regular[0].plans;
  assert.deepEqual(plans.map((plan) => plan.hours), ['11:00～6:00']);
  const loose = page('<p>会員・一般のお客様は別途ドリンクバー¥660をご注文いただきます。</p>', day, night);
  assert.equal(convertKanOcr([loose, loose]).reason, 'required-bar');
});

test('カラオケ館: 区分の見出しを 2 回繰り返して後ろが空の表は前半を使い、列がずれた表は読まない', () => {
  const twice = table('19:00~6:00', '1ドリンク制', ['学生', '会員', '一般', '学生', '会員', '一般'],
    [['月~木', '¥275', '¥380', '¥506', '', '', ''], ['金・土・日<br>祝・祝前日', '¥350', '¥480', '¥640', '', '', '']]);
  const html = page(day, twice);
  assert.equal(convertKanOcr([html, html]).regular[0].plans[1].days[0].prices.一般, 506);
  const shifted = table('19:00~6:00', '1ドリンク制', ['学生', '会員', '学生', '会員'],
    [['月~木', '¥275', '¥380', '¥506', ''], ['金・土・日<br>祝・祝前日', '¥350', '¥480', '¥640', '']]);
  assert.ok(convertKanOcr([page(day, shifted), page(day, shifted)]).reason);
});

test('カラオケ館: 曜日によって帯が狭くなる表は読まず、広くなるだけの表は読む', () => {
  const wider = page(day, night.replace('19:00~6:00', '19:00~6:00<br>(金・土・祝前日~7:00)'));
  assert.equal(convertKanOcr([wider, wider]).regular[0].plans[1].hours, '19:00～6:00');
  const shorter = page(day.replace('9:00~19:00', '9:00~19:00<br>(土・日・祝~18:00)'), night);
  assert.equal(convertKanOcr([shorter, shorter]).reason, 'band-note');
  const later = page(day.replace('9:00~19:00', '9:00~19:00<br>(月~金10:00~)'), night);
  assert.equal(convertKanOcr([later, later]).reason, 'band-note');
});

// ---- 実際の OCR の読み取り（tests/fixtures/kan_ocr.json）。金額は公式の画像と照らし合わせたもの ----

const fixture = JSON.parse(readFileSync(new URL('./fixtures/kan_ocr.json', import.meta.url), 'utf8')).shops;
const TODAY = '2026-11-26';
const real = (id) => convertKanOcr(fixture[id].reads, { today: TODAY });
const hoursOf = (result) => result.regular[0].plans.map((plan) => plan.hours);
const prices = (result, plan, row) => result.regular[0].plans[plan].days[row].prices;

test('カラオケ館: 「9:00～11:00はドリンクバーのみの営業」の時間は料金を出さない（kan-61, kan-133）', () => {
  const kan61 = real('kan-61');
  assert.deepEqual(hoursOf(kan61), ['11:00～18:00', '18:00～5:00']);
  assert.deepEqual(prices(kan61, 0, 0), { 学生会員: 170, 学生: 170, 会員: 260, 一般: 346 });
  assert.equal(prices(kan61, 1, 1).一般, 1040);
  // 曜日の欄が「月~金」「祝前日」の 2 行に分かれた表
  const kan133 = real('kan-133');
  assert.deepEqual(hoursOf(kan133), ['11:00～19:00', '19:00～5:00']);
  assert.deepEqual(kan133.regular[0].plans[0].days.map((row) => row.day), ['平日', '土日祝']);
  assert.deepEqual(prices(kan133, 0, 0), { 学生会員: 150, 学生: 150, 会員: 100, 一般: 133 });
});

test('カラオケ館: 表の中の人数割引や曜日の条件、知らない注記のある店は読まない（kan-222, kan-80, kan-77, kan-78）', () => {
  // 「3名様で25円引」などの見出し
  assert.equal(real('kan-222').reason, 'unknown-head');
  // 「1ドリンク制 or ドリンクバー+¥660 (土・日・祝 ドリンクバー+¥660)」は土日祝にドリンクバーが要るように読める
  assert.equal(real('kan-80').reason, 'unknown-head');
  assert.ok(real('kan-77').reason);
  // 「★11:00~18:00までの会員・一般のお客様はドリンクバーをご注文いただきます」
  assert.ok(real('kan-78').reason);
});

test('カラオケ館: ドリンクバーが必須の朝の表は、注記が表の下の行にあっても帯ごと外す（kan-22, kan-55）', () => {
  const kan22 = real('kan-22');
  assert.deepEqual(hoursOf(kan22), ['11:00～18:00', '18:00～6:00']);
  assert.deepEqual(prices(kan22, 0, 0), { 学生会員: 180, 学生: 180, 会員: 130, 一般: 173 });
  const kan55 = real('kan-55');
  assert.deepEqual(hoursOf(kan55), ['11:00～18:00', '18:00～5:00']);
  assert.deepEqual(kan55.solo[0].plans[0].days[1].prices, { 学生会員: 320, 学生: 320, 会員: 480, 一般: 606 });
});

test('カラオケ館: 崩れた見出しの読み方を、検算と 2 回の一致で確かめて読む（kan-205, kan-48, kan-5, kan-99, kan-231）', () => {
  // 「学生|会員|学生|会員」と読まれた夜の表。モーニングパックの告知は 30 分料金に掛からない
  const kan205 = real('kan-205');
  assert.deepEqual(hoursOf(kan205), ['9:00～18:00', '18:00～1:00']);
  assert.deepEqual(prices(kan205, 0, 0), { 学生会員: 240, 学生: 240, シニア: 240, 会員: 280, 一般: 373 });
  assert.deepEqual(prices(kan205, 1, 0), { 学生会員: 250, 学生: 250, 会員: 280, 一般: 373 });
  // 区分の見出しを 2 回繰り返し、後ろ半分に同じ金額を写した表
  const kan48 = real('kan-48');
  assert.deepEqual(prices(kan48, 0, 0), { 学生会員: 240, 学生: 240, 会員: 240, 一般: 320 });
  assert.deepEqual(prices(kan48, 1, 1), { 会員: 720, 一般: 960 });
  // 飲み物の見出しがなく、1 行目に時間帯と区分が並ぶ表
  assert.deepEqual(prices(real('kan-5'), 0, 0), { 学生会員: 55, 学生: 55, 会員: 85, 一般: 113 });
  // 時間帯が 2 行目の先頭に入った表
  const kan99 = real('kan-99');
  assert.deepEqual(hoursOf(kan99), ['11:00～18:00', '18:00～5:00']);
  assert.deepEqual(prices(kan99, 0, 0), { 会員: 150, 一般: 200 });
  // 「¥50(税込¥55)」は税込の金額
  assert.deepEqual(prices(real('kan-231'), 0, 0), { 学生会員: 55, 学生: 55, 会員: 110, 一般: 146 });
});

// ---- 作った表で、判定の境目を確かめる ----

const barOnly = (window) => `<div data-label="Text"><p>★${window}までは、ドリンクバーのみの営業となります。</p></div>`;

test('カラオケ館: ドリンクバーのみの時間が帯の始めからでなければ、店ごと読まない', () => {
  const trimmed = convertKanOcr([page(day, barOnly('9:00~11:00'), night), page(day, barOnly('9:00~11:00'), night)]);
  assert.deepEqual(hoursOf(trimmed), ['11:00～19:00', '19:00～6:00']);
  for (const window of ['10:00~11:00', '9:00~19:00', '18:00~20:00']) {
    const html = page(day, barOnly(window), night);
    assert.equal(convertKanOcr([html, html]).reason, 'bar-only', window);
  }
  const other = page(day, '<p>★平日はドリンクバーのみの営業です。</p>', night);
  assert.equal(convertKanOcr([other, other]).reason, 'bar-only');
});

const required = (range) => table(range, '会員・一般のお客様は別途ドリンクバー¥660をご注文いただきます。', ['学生', '会員', '一般'],
  [['月~金', '¥180', '¥150', '¥200'], ['土・日・祝', '¥240', '¥320', '¥426']]);
const headless = (range) => `<table><tr><th>${range}</th><th>学生</th><th>会員</th><th>一般</th></tr>`
  + `${body([['月~金', '¥180', '¥150', '¥200'], ['土・日・祝', '¥240', '¥320', '¥426']])}</table>`;
const later = (range) => day.replace('9:00~19:00', range);

test('カラオケ館: ドリンクバーが必須の注記が朝の帯以外の表や表の外にあれば読まない', () => {
  // 後ろの帯の表に付いた注記は、隣の表から移った読み違いかもしれない
  const moved = page(later('9:00~11:00'), required('11:00~19:00'), night);
  assert.equal(convertKanOcr([moved, moved]).reason, 'required-bar');
  // 飲み物の見出しのない表は、どれかの読み取りに必須の注記があれば読まない
  const plain = page(headless('9:00~11:00'), later('11:00~19:00'), night);
  assert.equal(convertKanOcr([plain, plain]).regular[0].plans[0].hours, '9:00～11:00');
  const noted = page(required('9:00~11:00'), later('11:00~19:00'), night);
  assert.equal(convertKanOcr([plain, noted]).reason, 'required-bar');
});

test('カラオケ館: 知らない見出しや表の外の文、曜日の欄の条件、会員と一般のない表は読まない', () => {
  const discount = page(table('9:00~19:00', '3名様で25円引', ['学生', '会員', '一般'],
    [['月~金', '¥150', '¥170', '¥226'], ['土・日・祝', '¥230', '¥280', '¥373']]), night);
  assert.equal(convertKanOcr([discount, discount]).reason, 'unknown-head');
  const note = page(day, '<p>★3名様以上のご利用で10%引きとなります。</p>', night);
  assert.equal(convertKanOcr([note, note]).reason, 'unknown-note');
  const label = page(day.replace('月~金', '月~金※学生不可'), night);
  assert.equal(convertKanOcr([label, label]).reason, 'day-label');
  const noGeneral = page(table('9:00~19:00', 'ドリンクバー付', ['学生', '会員'], [['月~金', '¥150', '¥170'], ['土・日・祝', '¥230', '¥280']]), night);
  assert.equal(convertKanOcr([noGeneral, noGeneral]).reason, 'ratio');
});

const split = (rows) => '<table><tr><th rowspan="2">19:00~6:00</th><th colspan="2">ドリンクバー付</th><th colspan="2">1ドリンク制</th></tr>'
  + `<tr><th>学生</th><th>会員</th><th>学生</th><th>会員</th></tr>${body(rows)}</table>`;

test('カラオケ館: 「学生|会員|学生|会員」の夜の表は、2 つの飲み物の見出しと検算が合うときだけ「学生 会員 一般」と読む', () => {
  const html = page(day, split([['月~木', '¥275', '¥380', '¥506', ''], ['金・土・日<br>祝・祝前日', '¥350', '¥480', '¥640', '']]));
  const result = convertKanOcr([html, html]);
  assert.deepEqual(prices(result, 1, 0), { 学生会員: 275, 学生: 275, 会員: 380, 一般: 506 });
  const wrong = page(day, split([['月~木', '¥275', '¥380', '¥500', ''], ['金・土・日<br>祝・祝前日', '¥350', '¥480', '¥640', '']]));
  assert.equal(convertKanOcr([wrong, wrong]).reason, 'ratio');
  const filled = page(day, split([['月~木', '¥275', '¥380', '¥506', '¥506'], ['金・土・日<br>祝・祝前日', '¥350', '¥480', '¥640', '¥640']]));
  assert.ok(convertKanOcr([filled, filled]).reason);
});

const notice = (text) => `<div data-label="Text"><p>${text}</p></div>`;

test('カラオケ館: 期間の告知は、期間がすべて終わるまで読まない（何か月先の期間も、年をまたぐ期間も）', () => {
  const newYear = '★年末年始(12/28~1/4)は土・日・祝料金となります。';
  // アプリの日付選びには上限がないので、22 日以上先の期間でも読まない（12/29 に平日の金額を出さない）
  assert.equal(withoutPeriods(newYear, '2026-11-26'), null);
  assert.equal(withoutPeriods(newYear, '2026-12-08'), null);
  assert.equal(withoutPeriods(newYear, '2027-01-04'), null);
  // 1/4 の夜の帯は 1/5 の朝まで続く
  assert.equal(withoutPeriods(newYear, '2027-01-05'), null);
  assert.equal(withoutPeriods(newYear, '2027-01-06'), '');
  const winter = '★12/1~1/5は特別料金となります。';
  assert.equal(withoutPeriods(winter, '2026-11-26'), null);
  assert.equal(withoutPeriods(winter, '2027-01-03'), null);
  assert.equal(withoutPeriods(winter, '2027-01-10'), '');
  assert.equal(withoutPeriods('★2027年3月1日~3月31日は特別料金となります。', '2026-11-26'), null);
  // 終わりの月が省かれた期間
  assert.equal(withoutPeriods('★12月28日~31日は休日料金となります。', '2026-12-10'), null);
  assert.equal(withoutPeriods('★12月28日~31日は休日料金となります。', '2026-11-01'), null);
  assert.equal(withoutPeriods('★12月28日~31日は休日料金となります。', '2027-01-02'), '');
  // 終わりの日付が読めない、ありえない日付は読まない
  assert.equal(withoutPeriods('★年末年始は特別料金となります。', '2026-11-26'), null);
  assert.equal(withoutPeriods('★2027年1月1日より料金改定となります。', '2026-11-26'), null);
  assert.equal(withoutPeriods('★2/30~3/1は特別料金となります。', '2026-11-26'), null);
  // 期間の告知のない文はそのまま
  assert.equal(withoutPeriods('★混雑時はご利用を制限', '2026-11-26'), '★混雑時はご利用を制限');
  const html = page(day, notice(newYear), night);
  assert.equal(convertKanOcr([html, html], { today: '2026-11-26' }).reason, 'period');
  assert.equal(convertKanOcr([html, html], { today: '2026-12-10' }).reason, 'period');
  assert.equal(convertKanOcr([html, html], { today: '2027-01-06' }).regular[0].plans.length, 2);
});

const picture = (alt) => `<div data-label="Image"><img alt="${alt}" /></div>`;

test('カラオケ館: 画像の説明（img の alt）の文も読み、ソフトクリームの挿絵のほかは店ごと読まない', () => {
  const reads = fixture['kan-61'].reads;
  const added = (part) => convertKanOcr(reads.map((html) => html + part), { today: TODAY });
  // 星形の囲みの人数割引が画像の説明に書かれた読み取り。<p> の文と同じく知らない文として外す
  assert.equal(added(picture("Starburst graphic with text '3名様以上で50円引' and '平日限定'")).reason, 'unknown-note');
  assert.equal(added(picture("Large graphic with text '0円' (0 Yen) in red and black")).reason, 'unknown-note');
  assert.equal(added('<div data-label="Image"><img /></div>').reason, 'unknown-note');
  assert.equal(added(`<div title="お一人様は+¥50"></div>`).reason, 'surcharge-note');
  // ソフトクリームの挿絵は料金に掛からない
  assert.equal(added(picture('Soft serve ice cream cone illustration')).reason, undefined);
  assert.equal(added(picture('Illustration of a swirl of soft-serve ice cream on a cone')).reason, undefined);
  // 表のセルの中の画像は、挿絵でもセルの形が合わないので読まない
  const cell = page(day.replace('<th>会員</th>', `<th>会員<img alt="Soft serve ice cream cone illustration" /></th>`), night);
  assert.equal(convertKanOcr([cell, cell]).reason, 'customer-head');
  const price = page(day.replace('<td>¥170</td>', `<td>¥170<img alt="3名様で25円引" /></td>`), night);
  assert.equal(convertKanOcr([price, price]).reason, 'price-text');
  // セルのタグ自身に書かれた説明も読む。セルは消さない（列がずれて別の列の金額にならない）
  const titled = page(day.replace('<td>¥170</td>', `<td title="3名様で25円引">¥170</td>`), night);
  assert.equal(convertKanOcr([titled, titled]).reason, 'price-text');
});

test('カラオケ館: 区分の欄が Object のもとからある名前でも、例外を出さずに読まない', () => {
  for (const name of ['constructor', 'toString', '__proto__', 'hasOwnProperty']) {
    const html = page(table('9:00~19:00', 'ドリンクバー付', [name, '会員', '一般'],
      [['月~金', '¥150', '¥170', '¥226'], ['土・日・祝', '¥230', '¥280', '¥373']]), night);
    assert.equal(convertKanOcr([html, html]).reason, 'customer-head');
    const headless = page(`<table><tr><th>9:00~19:00</th><th>${name}</th><th>会員</th><th>一般</th></tr>`
      + `${body([['月~金', '¥150', '¥170', '¥226']])}</table>`, night);
    assert.ok(convertKanOcr([headless, headless]).reason);
  }
});

const NOW = Date.UTC(2026, 10, 26, 3, 0) / 1000;
const DAY = 86400;
const stored = (fields) => ({ chain: 'カラオケ館', url: 'https://karaokekan.net/shop2price/image/61/0', key: 'a'.repeat(64),
  reads: [page(day, night), page(day, night)], checked_at: NOW - 30 * DAY, ...fields });

test('カラオケ館: 画像が変わったのに読めていない店と、長く画像を確かめていない店は配らない', () => {
  const at = { today: TODAY, now: NOW };
  assert.equal(convertKanShop(stored({ latest_key: 'a'.repeat(64), verified_at: NOW - DAY }), at).reason, undefined);
  assert.equal(convertKanShop(stored({ latest_key: 'b'.repeat(64), verified_at: NOW - DAY }), at).reason, 'image-changed');
  assert.equal(convertKanShop(stored({ latest_key: 'missing', verified_at: NOW - DAY }), at).reason, 'image-changed');
  assert.equal(convertKanShop(stored({ latest_key: 'a'.repeat(64), verified_at: NOW - (STALE_DAYS + 1) * DAY }), at).reason, 'stale');
  // 読んだ時刻にも画像は同じだった
  assert.equal(convertKanShop(stored({ latest_key: 'a'.repeat(64), checked_at: NOW - DAY }), at).reason, undefined);
});

test('カラオケ館: 確かめた記録がまだない店は 2026-11-26 の実行で確かめたとみなし、その回に取れなかった店はみなさない', () => {
  assert.equal(convertKanShop(stored({}), { today: TODAY, now: NOW }).reason, undefined);
  assert.equal(convertKanShop(stored({}), { today: '2026-12-10', now: Date.UTC(2026, 11, 10, 23, 0) / 1000 }).reason, 'stale');
  const unchecked = stored({ url: 'https://karaokekan.net/shop2price/image/205/0' });
  assert.equal(convertKanShop(unchecked, { today: TODAY, now: NOW }).reason, 'stale');
});

test('カラオケ館: 変換する日は JST の今日で、CONVERT_TODAY で差し替えられる', () => {
  assert.equal(todayJst({}, Date.UTC(2026, 10, 25, 16, 0)), '2026-11-26');
  assert.equal(todayJst({}, Date.UTC(2026, 10, 25, 14, 59)), '2026-11-25');
  assert.equal(todayJst({ CONVERT_TODAY: '2026-12-31' }, Date.UTC(2026, 10, 25, 16, 0)), '2026-12-31');
});
