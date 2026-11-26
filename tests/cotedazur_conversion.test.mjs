import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { convertCotedazur, convertCotedazurShops } from '../scripts/web/cotedazur.js';

const data = JSON.parse(readFileSync(new URL('./fixtures/cotedazur_prices.json', import.meta.url), 'utf8'));
// cd-10150: 昼夜の切り替えが 17:00 の店（通常料金 1 期間）。cd-10122: 年末年始の期間別の料金（11/26 に取ったもの）
const cases = JSON.parse(readFileSync(new URL('./fixtures/cotedazur_cases.json', import.meta.url), 'utf8'));
const [id, first] = Object.entries(data.shops)[0];
const TODAY = '2026-11-26';

const copy = (value) => JSON.parse(JSON.stringify(value));
function withPlan(shop, change) {
  const result = copy(shop);
  change(result.price_plan[0]);
  return result;
}
const convert = (shop, today = TODAY) => convertCotedazur(shop, { today });

test('1店舗だけ公式料金表の型が変わっても、正常店舗は変換する', () => {
  const records = {
    [id]: first,
    'cd-invalid': { shop_code: 99999, price_plan: [] },
  };
  const result = convertCotedazurShops(records, 0.5, { today: TODAY });
  assert.equal(result.accepted, 1);
  assert.equal(result.rejected.length, 1);
  assert.equal(result.rejected[0].id, 'cd-invalid');
  assert.ok(result.shops[id].regular.length > 0);
  assert.ok(result.shops[id].solo.length > 0);
});

test('公開料金が大量欠落するならfail-closedで配信を停止する', () => {
  const records = Object.fromEntries(Array.from({ length: 11 }, (_, i) => [
    `cd-${i}`, i === 0 ? first : { shop_code: i, price_plan: [] },
  ]));
  assert.throws(() => convertCotedazurShops(records, 0.9, { today: TODAY }), /safety threshold/);
  assert.throws(() => convertCotedazurShops({}, 0.9, { today: TODAY }), /safety threshold/);
});

test('公式JSONの見本の店がすべて変換できる', () => {
  const result = convertCotedazurShops(data.shops, 0.9, { today: TODAY });
  assert.equal(result.accepted, Object.keys(data.shops).length);
  assert.deepEqual(result.rejected, []);
});

test('配信ファイルの形は今までどおり（取得の記録は入れない）', () => {
  // 項目を外すと validate_data.py の「変更店舗が35%を超える」が全店に当たるので、形は変えない
  const result = convertCotedazurShops(data.shops, 0.9, { today: TODAY });
  for (const [key, shop] of Object.entries(result.shops)) {
    assert.deepEqual(Object.keys(shop).sort(),
      ['checked_at', 'price_plan', 'regular', 'shop_code', 'shop_name', 'solo'], key);
    assert.equal(shop.checked_at, data.shops[key].checked_at);
    assert.deepEqual(shop.price_plan, data.shops[key].price_plan);
  }
});

test('昼夜の切り替え時刻を公式の switching_time から読む', () => {
  const regular18 = convert(first).regular[0].plans.map((plan) => plan.hours);
  assert.deepEqual(regular18, ['開店～18:00', '18:00～閉店']);
  const shop = cases.shops['cd-10150'];
  assert.equal(shop.price_plan[0].switching_time, '17:00:00');
  const result = convert(shop);
  for (const table of [...result.regular, ...result.solo]) {
    assert.deepEqual(table.plans.map((plan) => plan.hours), ['開店～17:00', '17:00～閉店']);
  }
  // 17:00～18:00 は夜の料金（公式の平日夜 会員 30 分）
  const night = result.regular[0].plans[1].days.find((row) => row.day === '月火水木');
  assert.equal(night.prices.会員, Number(shop.price_plan[0].night_weekday[0].normal_30));
  assert.equal(convert(withPlan(shop, (plan) => { plan.switching_time = '17:30'; })).regular[0].plans[0].hours, '開店～17:30');
});

test('切り替え時刻や区切りが想定外の形なら、その店は配らない', () => {
  const shop = cases.shops['cd-10150'];
  for (const value of ['19:15:00', '', '18:00 ', '8:00:00', '14:00:00', '22:00:00', '18時', null, 18]) {
    assert.throws(() => convert(withPlan(shop, (plan) => { plan.switching_time = value; })), /切り替え時刻/, String(value));
  }
  assert.throws(() => convert(withPlan(shop, (plan) => { plan.switching_noon = 'OPEN '; })), /開店・閉店/);
  assert.throws(() => convert(withPlan(shop, (plan) => { plan.switching_night = '24:00'; })), /開店・閉店/);
});

test('年末年始の期間別の料金は今までどおり読まない', () => {
  const shop = cases.shops['cd-10122'];
  assert.equal(shop.price_plan.length, 4);
  assert.throws(() => convert(shop), /料金期間が複数/);
  // 期間の 1 つだけが残っても、通常料金でなければ読まない
  for (const plan of shop.price_plan) {
    assert.throws(() => convert({ ...shop, price_plan: [plan] }), /通常料金以外/, plan.label_name);
  }
});

test('通常料金でも、終わりの日時が書かれた期間や、まだ始まっていない期間は読まない', () => {
  const shop = cases.shops['cd-10150'];
  const at = (start, end) => withPlan(shop, (plan) => { plan.start_date_time = start; plan.end_date_time = end; });
  assert.ok(convert(at('2026-10-01 10:00', '')).regular.length);
  // アプリの日付選びには上限がないので、終わりが何か月先でも、終わりの後の料金が分からない期間は読まない
  assert.throws(() => convert(at('2026-10-01 10:00', '2026-12-20 23:59')), /終わりが決まっています/);
  assert.throws(() => convert(at('2026-10-01 10:00', '2027-03-31 23:59')), /終わりが決まっています/);
  assert.throws(() => convert(at('2026-10-01 10:00', '2026-11-30 10:00')), /終わりが決まっています/);
  assert.throws(() => convert(at('2026-10-01 10:00', '年明けまで')), /終わりが決まっています/);
  // 始まりは前日まで（今日の開店より前に始まっていること）
  assert.ok(convert(at('2026-11-25 10:00', '')).regular.length);
  assert.throws(() => convert(at('2026-11-26 10:00', '')), /まだ始まっていません/);
  assert.throws(() => convert(at('2026-12-01 10:00', '')), /まだ始まっていません/);
  // 空は公式の表示と同じく制限なし。公式の読み方（'/' 区切り、1 桁の月日）も受け付ける
  assert.ok(convert(at('', '')).regular.length);
  assert.ok(convert(at('2026/10/1 10:00', '')).regular.length);
  assert.throws(() => convert(at('2026/12/1', '')), /まだ始まっていません/);
  assert.throws(() => convert(at('10月1日', '')), /始まりを読めません/);
  assert.throws(() => convert(at('2026-02-30 10:00', '')), /始まりを読めません/);
});

test('期間の告知は、期間がすべて終わるまで読まない（何か月先の期間でも、年をまたぐ期間でも）', () => {
  const shop = cases.shops['cd-10150'];
  const noticed = (text) => withPlan(shop, (plan) => { plan.all_annotation_note += `\n・${text}`; });
  const yearEnd = noticed('12月31日～1月3日は年末年始料金となります。');
  // アプリでは 12/31 も選べるので、11/26 の時点で読まない
  assert.throws(() => convert(yearEnd, '2026-11-26'), /終わっていない期間の告知/);
  assert.throws(() => convert(yearEnd, '2026-12-11'), /終わっていない期間の告知/);
  assert.throws(() => convert(yearEnd, '2027-01-02'), /終わっていない期間の告知/);
  // 1/3 の夜は 1/4 の朝まで続くので、1/4 はまだ出さない。1/5 には 2026/12/31～2027/1/3 は終わっている
  assert.throws(() => convert(yearEnd, '2027-01-04'), /終わっていない期間の告知/);
  assert.ok(convert(yearEnd, '2027-01-05').regular.length);
  assert.throws(() => convert(noticed('2027年1月2日～1月3日は特別料金です。'), '2026-12-10'), /終わっていない期間の告知/);
  assert.ok(convert(noticed('2026年1月2日～1月3日は特別料金です。'), '2026-11-26').regular.length);
  // 曜日の付いた書き方も期間として読む
  const weekday = noticed('12月31日（木）～1月3日（日）は年末年始料金です。');
  assert.throws(() => convert(weekday, '2026-11-26'), /終わっていない期間の告知/);
  assert.ok(convert(weekday, '2027-01-05').regular.length);
  const listed = noticed('12月24日（木）、25日（金）は特別料金です。');
  assert.throws(() => convert(listed, '2026-11-26'), /終わっていない期間の告知/);
  // 「、」で並べた最後の日もその 1 日だけ。過ぎれば読む。「以降」が付けば終わりの分からない期間
  assert.ok(convert(listed, '2027-01-10').regular.length);
  assert.throws(() => convert(noticed('12月24日、25日以降は特別料金です。'), '2027-01-10'), /終わりの日を読めません/);
  const slash = noticed('12/24～12/25は特別料金です。');
  assert.throws(() => convert(slash, '2026-11-26'), /終わっていない期間の告知/);
  assert.ok(convert(slash, '2027-01-10').regular.length);
  // 「12.24」の形の日付と、言葉のない告知（イベント営業・料金が変わります）
  const dotted = noticed('12.24～12.25はイベント営業のため料金が変わります');
  assert.throws(() => convert(dotted, '2026-12-10'), /終わっていない期間の告知/);
  assert.ok(convert(dotted, '2027-01-10').regular.length);
  assert.throws(() => convert(noticed('12.24(木)～12.25(金)はクリスマス特別営業'), '2026-12-10'), /終わっていない期間の告知/);
  const sameMonth = noticed('12月11日、14日～17日は繁忙期のため特別料金です。');
  assert.throws(() => convert(sameMonth, '2026-11-26'), /終わっていない期間の告知/);
  assert.ok(convert(sameMonth, '2026-12-19').regular.length);
  // 終わりの日が読めない告知は出さない側に倒す
  assert.throws(() => convert(noticed('12月1日より料金を改定いたします。')), /終わりの日を読めません/);
  assert.throws(() => convert(noticed('年末年始は特別料金となります。')), /期間を読めません/);
  assert.throws(() => convert(noticed('クリスマスと繁忙日は料金が異なります。')), /期間を読めません/);
  assert.throws(() => convert(noticed('臨時で料金が変わる日があります。')), /期間を読めません/);
  const later = noticed('2026年12月26日から特別料金です。');
  assert.throws(() => convert(later, '2026-11-26'), /終わりの日を読めません/); // 始まりが先でも出さない
  assert.throws(() => convert(later, '2027-02-01'), /終わりの日を読めません/); // 始まった後も終わりが分からない
  // 告知の言葉がなくても、日付の書かれた行は期間として確かめる
  const dated = noticed('12月31日～1月3日は料金が異なります。');
  assert.throws(() => convert(dated, '2026-11-26'), /終わっていない期間の告知/);
  assert.throws(() => convert(noticed('12/31は営業時間が変わります。'), '2026-12-20'), /終わりの日を読めません/);
  // 期間の告知でない「限定」（学生グループ限定など）や、金額・時刻は今までどおり
  assert.ok(convert(noticed('学生グループ限定のフリータイムがあります。')).regular.length);
  assert.ok(convert(noticed('会員入会金 1,610円、受付 18:00~翌4:30')).regular.length);
});

test('終わりの側だけが書かれた告知は、その日に終わる期間として読む', () => {
  const shop = cases.shops['cd-10150'];
  const noticed = (text) => withPlan(shop, (plan) => { plan.all_annotation_note += `\n・${text}`; });
  // 12/15 の時点では 1/11 まで特別料金の期間に入っている（12/20 などに通常料金を出さない）
  for (const text of ['1月11日まで年末年始特別料金となります。', '年末年始（～1月11日）は特別料金です。',
    '1月11日（日）までは特別料金です。', '～1/11は特別料金']) {
    const shopWithNotice = noticed(text);
    assert.throws(() => convert(shopWithNotice, '2026-12-15'), /終わっていない期間の告知/, text);
    assert.throws(() => convert(shopWithNotice, '2027-01-12'), /終わっていない期間の告知/, text); // 1/11 の夜の続き
    assert.ok(convert(shopWithNotice, '2027-01-13').regular.length, text);
  }
});

test('税込と書かれていない、または税抜の料金は読まない', () => {
  const shop = cases.shops['cd-10150'];
  assert.throws(() => convert(withPlan(shop, (plan) => {
    plan.all_annotation_note = plan.all_annotation_note.replace('税込', '');
  })), /税込と書かれていません/);
  assert.throws(() => convert(withPlan(shop, (plan) => {
    plan.all_annotation_note = plan.all_annotation_note.replace('税込', '税抜');
  })), /税抜/);
  assert.throws(() => convert(withPlan(shop, (plan) => { plan.solo_all_annotation_note += '\n・価格は税別です。'; })), /税抜/);
  assert.throws(() => convert(withPlan(shop, (plan) => { plan.label_name = '通常料金（税別）'; })), /通常料金以外|税抜/);
});

test('料金なしの記録は変換しない', () => {
  const record = { shop_code: 10457, shop_name: 'x', price_plan: [], no_price: '公式の料金JSONなし', checked_at: 1 };
  assert.throws(() => convert(record), /公式の料金表なし/);
});

test('1ドリンク制の時間帯は、まねきねこと同じ「1ドリンク別」にする', () => {
  const result = convert(cases.shops['cd-10150']);
  const drinks = result.regular[0].plans.map((plan) => plan.drink);
  const official = ['noon_drink', 'night_drink'].map((key) => cases.shops['cd-10150'].price_plan[0][key][0].drink_type);
  official.forEach((kind, index) => {
    if (/1ドリンク/.test(kind)) assert.equal(drinks[index], '1ドリンク別');
    else assert.equal(drinks[index], kind);
  });
});

test('変換する日の形式が不正なら止める', () => {
  assert.throws(() => convert(first, '2026/11/26'), /変換する日の形式/);
});
