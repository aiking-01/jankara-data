// OCR が書き出した HTML の料金表を engine.js の regular の形へ変換する。DOM に依存しない。
// OCR は数字をよく読むが、表の列を足したり行を重ねたりすることがある。形が少しでも想定と違えば店ごと読まない。
// 2 回読んだ店は、2 回の結果が完全に一致したときだけ使う。

import { dayLabels, parseDays } from './bigecho.js';

const CUSTOMERS = ['学生会員', '学生', '会員', 'シニア', '一般'];
const MAX_SLOT_PRICE = 1500;

const norm = (text) => (text ?? '').normalize('NFKC').replace(/\s+/g, '');
const textOf = (html) => html.replace(/<br\s*\/?>/gi, ' ').replace(/<[^>]+>/g, '')
  .replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&nbsp;/g, ' ');

// <table> を行ごとのセルの配列にする。colspan と rowspan は見出しの判定に使わないので展開しない
export function tablesOf(html) {
  return [...html.matchAll(/<table>([\s\S]*?)<\/table>/g)].map((table) => [...table[1].matchAll(/<tr>([\s\S]*?)<\/tr>/g)]
    .map((row) => [...row[1].matchAll(/<(t[hd])([^>]*)>([\s\S]*?)<\/t[hd]>/g)]
      .map((cell) => ({ head: cell[1] === 'th', rowspan: /rowspan/.test(cell[2]), text: norm(textOf(cell[3])) }))));
}

const RANGE = /^(\d{1,2}:\d{2})~(\d{1,2}:\d{2})/;
const KAN_PRICE = /^¥(\d{1,3}(?:,\d{3})?)$/;
const KAN_CUSTOMER = {
  学生: ['学生会員', '学生'], '学生・シニア': ['学生会員', '学生', 'シニア'], シニア: ['シニア'], 会員: ['会員'], 一般: ['一般'],
};

// 「会員・一般のお客様は別途ドリンクバー¥680をご注文いただきます」は注文が必須で、表の金額だけでは安く見える
const REQUIRED_BAR = /別途ドリンクバー/;

// カラオケ館の 1 つの表。1 行目に時間帯と飲み物の条件、2 行目に区分、その下に曜日ごとの金額が並ぶ
// 飲み物の条件は、学生の列だけに掛かる見出しを OCR が全部の列に広げることがあり、2 回とも同じ誤りになるので使わない。
// 店と時間帯と区分で「ドリンクバー付」と「1ドリンク制」が分かれるので、込みと誤解されないよう公式の表へ案内する
const UNKNOWN_DRINK = 'ドリンク条件は公式の表で確認';
function readKanTable(rows) {
  let [first, second, ...body] = rows;
  if (!first || !second || !body.length) return { reason: 'table-shape' };
  const range = RANGE.exec(first[0]?.text ?? '');
  if (!range) return { reason: 'no-range' };
  if (narrowed(range[1], range[2], first[0].text.slice(range[0].length))) return { reason: 'band-note' };
  // 曜日の列の上の空いた見出しと、1 行目からずれて入った飲み物の見出しは読み飛ばす。
  // 区分の見出しが途中で空いているときは列がずれているので読まない
  const lead = second[0]?.text ?? '';
  let customers = (lead === '' || /ドリンク/.test(lead) ? second.slice(1) : second).map((cell) => cell.text);
  // 飲み物の見出しごとに「学生 会員 一般」を 2 回繰り返し、後ろ半分が空の読み方がある。前半だけを使う
  const half = customers.length / 2;
  if (Number.isInteger(half) && half > 1 && customers.slice(0, half).join() === customers.slice(half).join()
    && body.every((row) => row.length === customers.length + 1 && row.slice(1 + half).every((cell) => cell.text === ''))) {
    customers = customers.slice(0, half);
    body = body.map((row) => row.slice(0, 1 + half));
  }
  if (!customers.length || !customers.every((name) => KAN_CUSTOMER[name]) || new Set(customers).size !== customers.length) {
    return { reason: 'customer-head' };
  }
  const requiredBar = first.some((cell) => REQUIRED_BAR.test(cell.text));
  const columns = [];
  for (const row of body) {
    const [label, ...cells] = row;
    const days = parseDays(label?.text ?? '');
    if (!days) return { reason: 'day-label' };
    if (cells.length !== customers.length) return { reason: 'price-count' };
    const prices = {};
    for (const [index, cell] of cells.entries()) {
      const match = KAN_PRICE.exec(cell.text);
      if (!match) return { reason: 'price-text' };
      const value = Number(match[1].replace(',', ''));
      if (value < 1 || value > MAX_SLOT_PRICE) return { reason: 'price-range' };
      for (const name of KAN_CUSTOMER[customers[index]]) prices[name] = value;
    }
    columns.push({ days, prices });
  }
  const mapped = dayLabels(columns.map((column) => column.days));
  if (!mapped || mapped.partial || mapped.holidayUnknown) return { reason: 'days' };
  const days = columns.map((column, index) => ({ day: mapped.labels[index], prices: column.prices }));
  const plan = { name: '30分', hours: `${range[1]}～${range[2]}`, last_entry: '', days, drink: UNKNOWN_DRINK };
  return { plan, requiredBar, start: range[1], end: range[2] };
}

const minutesOf = (clock) => clock.split(':').reduce((hours, minutes) => Number(hours) * 60 + Number(minutes));

function spanOf(start, end) {
  const from = minutesOf(start);
  let to = minutesOf(end);
  if (to <= from) to += 1440;
  return [from, to];
}

// 「★土・日・祝(OPEN~19:00)の、お一人様でのご利用は上記金額+¥50となります」。曜日と区分と時間帯は省かれることがある
const SOLO_NOTE = /★?(会員・一般)?\(?(土・?日・?祝)?[(:]?((?:O?OPEN|\d{1,2}:\d{2})~\d{1,2}:\d{2})?\)?の?、?(?:お一人様|ひとりカラオケ)(?:での?|の)ご利用は上記(?:金額|料金)\+¥(\d{1,3})となります/g;
const SOLO_WORD = /お一人様|ひとりカラオケ|お?1[人名]様での/;
const DARTS = /ダーツのご利用は[^★。]*?となります/g;

// 「18:00~2:00(金・祝前日~3:00)(土~4:00)」の括弧は曜日ごとの時刻。帯より広いだけなら、帯の中の金額は変わらない。
// 「9:00~11:00(土・日・祝~10:00)」のように狭い曜日があると、その曜日の残りは次の帯の金額になるので読まない
function narrowed(start, end, rest) {
  const [from, to] = spanOf(start, end);
  const notes = [...rest.matchAll(/\(([^()]*)\)/g)].map((match) => match[1]);
  if (rest.replace(/\([^()]*\)/g, '') !== '') return true;
  for (const note of notes) {
    const parts = [...note.matchAll(/(\d{1,2}:\d{2})?~(\d{1,2}:\d{2})?/g)];
    if (!parts.length || parts.some((part) => !part[1] && !part[2])) return true;
    for (const [, noteFrom, noteTo] of parts) {
      if (noteFrom && minutesOf(noteFrom) > from) return true;
      if (noteTo && spanOf(start, noteTo)[1] < to) return true;
    }
  }
  return false;
}

// 1 人で使うときの上乗せを書いた注記を読む。時間帯が表の区切りと合わない注記や、ほかの上乗せがあれば読まない
function soloNotes(text, spans) {
  const notes = [];
  for (const match of text.matchAll(SOLO_NOTE)) {
    const [, customers, days, window, extra] = match;
    if (!window) return { reason: 'solo-window' };
    const [from, to] = window.split('~');
    const start = /OPEN/.test(from) ? Math.min(...spans.map(([begin]) => begin)) : minutesOf(from);
    let end = minutesOf(to);
    if (end <= start) end += 1440;
    const inside = spans.map(([begin, finish]) => start <= begin && finish <= end);
    const touches = spans.map(([begin, finish]) => begin < end && start < finish);
    if (inside.some((value, index) => value !== touches[index]) || !inside.some(Boolean)) return { reason: 'solo-window' };
    notes.push({ inside, days: days ? '土日祝' : null, customers: customers ? ['会員', '一般'] : null, extra: Number(extra) });
  }
  const rest = text.replace(SOLO_NOTE, '').replace(DARTS, '');
  if (SOLO_WORD.test(rest) || /上記[^★。]*?\+¥?\d/.test(rest)) return { reason: 'surcharge-note' };
  return { notes };
}

// 通常の表に注記の上乗せを足して 1 人用の表を作る。曜日の行が注記の曜日と合わなければ読まない
function soloPlans(plans, notes) {
  const solo = plans.map((plan) => ({ ...plan, days: plan.days.map((row) => ({ ...row, prices: { ...row.prices } })) }));
  for (const note of notes) {
    for (const [index, plan] of solo.entries()) {
      if (!note.inside[index]) continue;
      for (const row of plan.days) {
        if (note.days && row.day !== note.days) {
          if (row.day !== '平日') return null;
          continue;
        }
        for (const name of Object.keys(row.prices)) {
          if (!note.customers || note.customers.includes(name)) row.prices[name] += note.extra;
        }
      }
    }
  }
  return solo;
}

// 1 回分の読み取りを regular と solo にする
function readKan(html) {
  const text = norm(textOf(html));
  if (!/30分/.test(text)) return { reason: 'not-30min' };
  if (/税[別抜]/.test(text)) return { reason: 'tax-excluded' };
  const tables = tablesOf(html);
  if (!tables.length) return { reason: 'no-table' };
  const plans = [];
  const spans = [];
  const requiredBar = [];
  for (const rows of tables) {
    const table = readKanTable(rows);
    if (table.reason) return table;
    const span = spanOf(table.start, table.end);
    if (spans.some(([from, to]) => span[0] < to && from < span[1])) return { reason: 'band-overlap' };
    spans.push(span);
    plans.push(table.plan);
    requiredBar.push(table.requiredBar);
  }
  // ドリンクバーの注文が必須の時間帯は出さない。どの表の注記か分からなければ店ごと読まない
  if (REQUIRED_BAR.test(text) && !requiredBar.some(Boolean)) return { reason: 'required-bar' };
  const { notes, reason } = soloNotes(text, spans);
  if (reason) return { reason };
  const solo = notes.length ? soloPlans(plans, notes) : [];
  if (!solo) return { reason: 'solo-days' };
  const kept = (list) => list.filter((_, index) => !requiredBar[index]);
  if (!kept(plans).length) return { reason: 'required-bar' };
  const table = (list) => [{ band: '30分', customers: CUSTOMERS, plans: kept(list), note: '' }];
  return { regular: table(plans), solo: solo.length ? table(solo) : [] };
}

// 読み取りが 1 回なら確かめる手段がないので使わない。2 回とも同じ結果のときだけ返す
export function convertKanOcr(reads) {
  if (!reads || reads.length < 2) return { reason: 'single-read' };
  const results = reads.map(readKan);
  const failed = results.find((result) => result.reason);
  if (failed) return failed;
  const [first, ...others] = results.map((result) => JSON.stringify([result.regular, result.solo]));
  if (others.some((other) => other !== first)) return { reason: 'reads-differ' };
  return results[0];
}
