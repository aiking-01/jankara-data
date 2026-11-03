// OCR が書き出した HTML から金額を作る処理を確かめる
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { convertKanOcr } from '../scripts/web/ocr.js';

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
  assert.equal(first.drink, '');
  assert.equal(second.hours, '19:00～6:00');
  assert.equal(second.days[1].prices.一般, 640);
  assert.deepEqual(result.solo, []);
});

test('カラオケ館: 読み取りが 1 回だけか、2 回で金額が違えば使わない', () => {
  const html = page(day, night);
  assert.equal(convertKanOcr([html]).reason, 'single-read');
  assert.equal(convertKanOcr([html, html.replace('¥226', '¥228')]).reason, 'reads-differ');
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
