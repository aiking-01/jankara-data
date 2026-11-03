// まねきねこの料金表 PDF から取り出した文字の並びを engine.js の regular / solo の形へ変換する。DOM に依存しない。
// 誤った金額を出さないことを最優先にする。表の形が少しでも想定と違えば店ごと読まず、公式の料金表へ案内する。
// 対象は 30 分料金だけ。フリータイム、パック、ZERO カラ、小中学生コース、まふは計算しない。
// PDF は複数ページのものが多く、1 ページ目は 2 名以上の料金で、1 人料金が別のページにある店がある。
// 文字は [文字列, 左, 上, 右, 下]。座標はページごとで、y は下向き。

import { dayLabels, parseDays } from './bigecho.js';

const CUSTOMERS = ['学生会員', '学生', '会員', 'シニア', '一般'];
const TIME = /^翌?\d{1,2}:\d{2}$/;
const PRICE = /^\d{1,3}(?:,\d{3})*$/;
const EXTRA = /^\+\d{1,3}(?:,\d{3})*$/;
const CLOSED = '未実施';
const MAX_SLOT_PRICE = 1500; // 30 分でこれより高ければ別の行を読んでいる
const COLUMN_GAP = 40; // 金額と曜日の見出しの中心がこれより離れていれば別の列

const norm = (text) => (text ?? '').normalize('NFKC').replace(/\s+/g, '');
const yen = (text) => Number(text.replace(/[+,]/g, ''));
const centerX = (token) => (token.left + token.right) / 2;
const centerY = (token) => (token.top + token.bottom) / 2;

// 上から別の文字を貼って直した PDF がある。文字は描かれた順に並んでいるので、下に隠れた見出しを捨てる。
// 隠れているのが数字なら、どちらが正しい金額か確かめようがないので null を返す
function readTokens(raw) {
  const tokens = raw.map(([text, left, top, right, bottom]) => ({ text: norm(text), left, top, right, bottom }))
    .filter((token) => token.text);
  const covers = (above, below) => {
    const width = Math.min(above.right, below.right) - Math.max(above.left, below.left);
    const height = Math.min(above.bottom, below.bottom) - Math.max(above.top, below.top);
    return width > 0.5 * Math.min(above.right - above.left, below.right - below.left)
      && height > 0.5 * Math.min(above.bottom - above.top, below.bottom - below.top);
  };
  const visible = [];
  for (let i = 0; i < tokens.length; i += 1) {
    const cover = tokens.slice(i + 1).find((later) => covers(later, tokens[i]));
    if (!cover) visible.push(tokens[i]);
    else if (/\d/.test(tokens[i].text) && cover.text !== CLOSED) return null;
  }
  return visible;
}

// 「2」「,200」のように切れた金額をつなぐ。列の間はこれよりずっと離れている
function joinNumbers(tokens) {
  const sorted = [...tokens].sort((a, b) => a.left - b.left);
  const joined = [];
  for (const token of sorted) {
    const before = joined.findLast((other) => /^[+\d,]+$/.test(other.text)
      && Math.abs(centerY(other) - centerY(token)) < 6 && token.left - other.right > -2 && token.left - other.right < 4);
    if (before && /^[\d,]+$/.test(token.text)) {
      before.text += token.text;
      before.right = token.right;
      before.top = Math.min(before.top, token.top);
      before.bottom = Math.max(before.bottom, token.bottom);
    } else joined.push({ ...token });
  }
  return joined.sort((a, b) => a.top - b.top || a.left - b.left);
}

// 色の帯にある「12:00 ~ 18:00」。同じ高さに並ぶ時刻の組のうち、ラベルより上で最も近いものを返す。
// フリータイムの欄の時刻は縦に並ぶので組にならない
function bandHeader(tokens, label) {
  const times = tokens.filter((token) => TIME.test(token.text) && token.left < 210 && token.bottom <= label.top + 1);
  const pairs = [];
  for (const first of times) {
    const others = times.filter((other) => other !== first && other.left > first.left
      && other.left - first.left < 100 && Math.abs(centerY(other) - centerY(first)) < 4);
    if (others.length === 1) pairs.push([first, others[0]]);
  }
  if (!pairs.length) return null;
  return pairs.reduce((best, pair) => (pair[0].top > best[0].top ? pair : best));
}

const clockOf = (text) => {
  const [hours, minutes] = text.replace('翌', '').split(':').map(Number);
  return hours * 60 + minutes;
};
const rangeOf = (pair) => `${pair[0].text}～${pair[1].text.replace('翌', '')}`;

// 帯の時刻の右にある説明。30 分料金に関わるかもしれない説明がある表は読まない
const HARMLESS_NOTE = /^(?:※?(?:[^※]*(?:フリー|FT)[^※]*|[^※]*混雑[^※]*|最終受付[^※]*|(?:お1人様・非会員様|おひとり様利用)は一般料金[^※]*)?|ワンドリンクorドリンクバー|※?(?:ワン)?ドリンクオーダー制)$/;
const CHANGES_PRICE = /30分|一律|追加|\+\d|\d+月\d+日|期間/;
const WIDER_HOURS = /^※?(?:週末|土日祝日?|金・?土・?祝前?|金曜・土曜)(?:\([^)]*\))?は(\d{1,2}:\d{2})~?(翌?\d{1,2}:\d{2})$/;
const SECTION_HEAD = /^(?:会員|会員\(お1人様\)|一般|一般様|一般\(お1人様\)|1名様|お1人様料金|おひとり様料金|お(?:ひとり|一人)様\/一般|非会員様・?1名様利用時.*)$/;
const SOLO_TITLE = /^(?:お?1人(?:様)?料金|お1人様用|1名様料金)$/;
const LATER_CLOSE = /^※?(?:週末|金・?土・?祝前?)は(翌\d{1,2}:\d{2})まで$/;
const ALERT_WORDS = /特別料金|特別期間|年末年始|お盆|GW|ゴールデンウィーク|繁忙/g;

const spanOf = (from, to) => {
  const start = clockOf(from);
  let end = clockOf(to);
  if (to.startsWith('翌') || end <= start) end += 1440;
  return [start, end];
};

function noteAllows(tokens, pair) {
  const line = tokens.filter((token) => token.left > pair[1].right && token.top < pair[1].bottom && token.bottom > pair[1].top && !SECTION_HEAD.test(token.text) && !token.text.startsWith('≪'))
    .sort((a, b) => a.left - b.left).map((token) => token.text).join('');
  const note = line.replace(/\(フリー[^)]*\)/g, '').replace(/ワンドリンクorドリンクバー$/, '');
  // 「※金・土・祝前は翌3:00まで」のように終わりが帯より遅いだけなら、帯の中の金額は変わらない
  const later = LATER_CLOSE.exec(note);
  if (later && spanOf(pair[0].text, later[1])[1] >= spanOf(pair[0].text, pair[1].text)[1]) return true;
  // 「※週末は18:00 ~ 翌7:00」のように帯より広いだけなら、帯の中の金額は変わらない
  const wider = WIDER_HOURS.exec(note);
  if (wider) {
    const [from, to] = spanOf(pair[0].text, pair[1].text);
    const [noteFrom, noteTo] = spanOf(wider[1], wider[2]);
    if (noteFrom <= from && noteTo >= to) return true;
  }
  return (HARMLESS_NOTE.test(note) && !CHANGES_PRICE.test(note)) || onlyHours(note);
}

// フリータイムやパックだけの説明を除くと、曜日と時刻しか残らない注記か。帯の時刻が曜日で変わるだけで、金額は変わらない。
// 使える文字を限っているので、金額や区分の語が混ざると通らない
const OTHER_PLANS = /\([^()]*(?:フリー|パック|FT)[^()]*\)|※[^※]*(?:フリー|パック|FT)[^※]*/g;
// 帯の名前や、金額に関わらない案内
const PLAIN_NOTE = /^(?:\.|昼料金|夜料金|室料|※オプションワンドリンクオーダーorドリンクバー|※入室処理後のコース変更は出来かねます。|※15歳以下18:00まで。小学生、未就学生のみでの入店不可)$/;
const HOURS_ONLY = /^[0-9:~時翌※()、・.週末平金土日月木祝前曜昼夜の受付営業間よりまではです]*$|^[※()]*$/;
function onlyHours(note) {
  if (PLAIN_NOTE.test(note.replace(OTHER_PLANS, ''))) return true;
  const rest = note.replace(OTHER_PLANS, '').replace(/CLOSE|OPEN/g, '時');
  if (rest && !/\d/.test(rest)) return false;
  return HOURS_ONLY.test(rest) && !/(?:^|[^0-9:])\d時間/.test(rest.replace('24時間', ''));
}

// 帯の中で最初に現れる金額の行。ラベルのすぐ下にある
function priceRow(tokens, header, label) {
  const isCell = (token) => PRICE.test(token.text) || EXTRA.test(token.text) || token.text === CLOSED;
  const below = tokens.filter((token) => isCell(token) && token.top > header.bottom && token.right - token.left > 12);
  if (!below.length) return null;
  const first = Math.min(...below.map(centerY));
  if (first > label.bottom + 40) return null;
  return below.filter((token) => Math.abs(centerY(token) - first) < 7);
}

function nearest(items, token) {
  return items.reduce((best, item) => (!best || Math.abs(centerX(item) - centerX(token)) < Math.abs(centerX(best) - centerX(token)) ? item : best), null);
}

const SECTION_NAMES = {
  会員: '会員', '会員(お1人様)': '会員', 一般: '一般', 一般様: '一般', '一般(お1人様)': '一般', '1名様': '1人', お1人様料金: '1人', おひとり様料金: '1人', 'おひとり様/一般': '1人一般', 'お一人様/一般': '1人一般',
};

const dayText = (text) => text.replace(/^平日:/, '').replace(/([金祝前])[昼夜]/g, '$1');

// 1 つの帯を { from, to, sections: { 会員: [{days, price}], ... }, plus: { solo, general } } にする
function readBand(tokens, label) {
  const pair = bandHeader(tokens, label);
  if (!pair) return { reason: 'no-band-time' };
  if (!noteAllows(tokens, pair)) return { reason: 'band-note' };
  const header = { bottom: Math.max(pair[0].bottom, pair[1].bottom) };
  const row = priceRow(tokens, header, label);
  if (!row) return { reason: 'no-price-row' };
  const rowTop = Math.min(...row.map((token) => token.top));
  const between = tokens.filter((token) => token.top >= pair[0].top - 3 && token.bottom <= rowTop + 2 && token.left > label.right);
  const dayHeads = between.filter((token) => parseDays(dayText(token.text)) && !/^(?:1名|非会員)/.test(token.text));
  const sectionHeads = between.filter((token) => SECTION_HEAD.test(token.text));
  if (!dayHeads.length) return { reason: 'no-day-heads' };
  if (!sectionHeads.some((token) => SECTION_NAMES[token.text] === '会員')) return { reason: 'no-member-head' };

  const sections = {};
  const plus = {};
  for (const cell of [...row].sort((a, b) => a.left - b.left)) {
    if (EXTRA.test(cell.text)) {
      const head = nearest(between.filter((token) => /^(?:1名様?|非会員様?)$/.test(token.text) && token.left > cell.left - COLUMN_GAP), cell);
      if (!head || Math.abs(centerX(head) - centerX(cell)) > COLUMN_GAP) return { reason: 'extra-without-head' };
      plus[head.text.startsWith('1名') ? 'solo' : 'general'] = yen(cell.text);
      continue;
    }
    const head = nearest(dayHeads, cell);
    if (Math.abs(centerX(head) - centerX(cell)) > COLUMN_GAP) return { reason: 'price-without-head' };
    const section = nearest(sectionHeads, cell).text;
    if (section.startsWith('非会員')) return { reason: 'price-in-extra-section' };
    const name = SECTION_NAMES[section];
    const price = cell.text === CLOSED ? null : yen(cell.text);
    if (price !== null && (price < 1 || price > MAX_SLOT_PRICE)) return { reason: 'price-out-of-range' };
    (sections[name] ??= []).push({ days: dayText(head.text), price, x: centerX(cell) });
  }
  // 見出しの数と金額の数が合わなければ読み落としがある
  if (!sections.会員?.length) return { reason: 'no-member-price' };
  for (const cells of Object.values(sections)) {
    if (new Set(cells.map((cell) => cell.days)).size !== cells.length) return { reason: 'same-head' };
  }
  const priceColumns = Object.values(sections).reduce((sum, cells) => sum + cells.length, 0);
  const headsOfDay = dayHeads.length;
  if (priceColumns !== headsOfDay) return { reason: `price-count ${priceColumns}/${headsOfDay}` };
  return { from: pair[0].text, to: pair[1].text, range: rangeOf(pair), sections, plus };
}

// 同じ曜日の並びの金額を engine の days にする
function daysRows(cells, pick) {
  const mapped = dayLabels(cells.map((cell) => parseDays(cell.days)));
  if (!mapped || mapped.partial || mapped.holidayUnknown) return null;
  const rows = cells.map((cell, index) => ({ day: mapped.labels[index], prices: pick(cell) }));
  const rank = (row) => (row.day.includes('祝前') ? 0 : row.day.includes('祝') ? 1 : 2);
  return rows.sort((a, b) => rank(a) - rank(b));
}

const sameDays = (a, b) => a.map((cell) => norm(cell.days)).join() === b.map((cell) => norm(cell.days)).join();

const textOf = (tokens) => tokens.map((token) => token.text).join('');

// 1 ページの表を時間帯ごとに読む。{ bands, whole } か { reason } を返す
function readTable(raw) {
  const visible = readTokens(raw);
  if (!visible) return { reason: 'covered-number' };
  const tokens = joinNumbers(visible);
  const labels = tokens.filter((token) => token.text === '30分' && token.left < 180);
  if (!labels.length) return { reason: 'no-30min-label' };
  const whole = textOf(visible); // 描かれた順なら文の並びが崩れない
  if (/税[別抜]/.test(whole)) return { reason: 'tax-excluded' };
  const bands = [];
  for (const label of labels) {
    const band = readBand(tokens, label);
    if (band.reason) return band;
    bands.push(band);
  }
  // 時間帯が重なる表は、どちらの料金か決められない
  const spans = bands.map((band) => spanOf(band.from, band.to));
  for (let i = 0; i < spans.length; i += 1) {
    for (let j = i + 1; j < spans.length; j += 1) {
      if (spans[i][0] < spans[j][1] && spans[j][0] < spans[i][1]) return { reason: 'band-overlap' };
    }
  }
  return { bands, whole };
}

// 表の題が 1 人料金か。「お1人様料金」は列の見出しにも使われるので、最初の時間帯より上にあるものだけを題とみなす
const looksSolo = (raw) => {
  const tokens = readTokens(raw) ?? [];
  const firstTime = Math.min(...tokens.filter((token) => TIME.test(token.text)).map((token) => token.top));
  return tokens.some((token) => token.text === '会員(お1人様)' || (SOLO_TITLE.test(token.text) && token.top < firstTime));
};

// 1 人料金の表を engine の solo の形にする。時間帯が通常の表と揃わなければ使わない
function soloFromTable(table, plans) {
  const list = [];
  for (const band of table.bands) {
    const members = band.sections.会員;
    const general = band.sections.一般;
    if (general && !sameDays(members, general)) return null;
    const days = daysRows(members, (cell) => {
      const prices = {};
      if (cell.price) prices.会員 = cell.price;
      const other = general?.find((item) => item.days === cell.days)?.price;
      if (other) prices.一般 = other;
      return prices;
    });
    if (!days) return null;
    list.push({ name: '30分', hours: band.range, last_entry: '', days });
  }
  const hours = (items) => items.map((item) => item.hours).sort().join();
  return hours(list) === hours(plans) ? list : null;
}

// PDF のページごとの文字の並びから { regular, solo, flat, minStay } か { reason } を返す
export function convertManeki(tables) {
  const pages = (tables ?? []).filter((raw) => raw?.length);
  if (!pages.length) return { reason: 'no-text' };
  // 1 人料金の表は別のページにある。「会員(お1人様)」の見出しだけの表は、通常の表がなければ 1 人用か判断できない
  const soloPages = pages.filter(looksSolo);
  const regularPages = pages.filter((raw) => !soloPages.includes(raw));
  if (!regularPages.length) return { reason: 'only-solo-table' };
  if (regularPages.length > 1) return { reason: 'several-tables' };
  if (soloPages.length > 1) return { reason: 'several-solo-tables' };
  const main = readTable(regularPages[0]);
  if (main.reason) return main;
  const { bands, whole } = main;

  if (/土日祝日?の(?:お1人|おひとり)/.test(whole)) return { reason: 'solo-by-day' };
  const soloIsGeneral = /(?:お?1人様?|1名様?|おひとり様)[^※]*一般料金/.test(whole);
  const soloDiffers = /(?:お?1人様?|1名様?|おひとり様)[^※]*料金が異なる/.test(whole) || /名様以上/.test(whole);
  const flatOnce = /一律価格[^※]*一度のみ/.test(whole);
  const drink = /(?:1|ワン)?ドリンクオーダー制/.test(whole) ? '1ドリンク別' : '';

  const flats = { solo: new Set(), general: new Set() };
  const plans = [];
  let soloPlans = [];
  for (const band of bands) {
    const members = band.sections.会員;
    const general = band.sections.一般 ?? band.sections['1人一般'];
    const solo = band.sections['1人'] ?? band.sections['1人一般'];
    if ((general && !sameDays(members, general)) || (solo && !sameDays(members, solo))) return { reason: 'section-days-differ' };
    const hasPlus = band.plus.solo !== undefined || band.plus.general !== undefined;
    if (hasPlus && !flatOnce) return { reason: 'flat-unclear' };
    if (hasPlus) {
      flats.solo.add(band.plus.solo);
      flats.general.add(band.plus.general);
    }
    const days = daysRows(members, (cell) => {
      if (cell.price === null) return {};
      const prices = { 会員: cell.price };
      const other = general?.find((item) => item.days === cell.days);
      if (other?.price) prices.一般 = other.price;
      else if (hasPlus && band.plus.general !== undefined) prices.一般 = cell.price;
      return prices;
    });
    if (!days) return { reason: 'unreadable-days' };
    plans.push({ name: '30分', hours: band.range, last_entry: '', days, drink });
    const soloCells = solo ?? (soloIsGeneral ? general : null);
    if (soloCells) {
      const soloDays = daysRows(members, (cell) => {
        const value = soloCells.find((item) => item.days === cell.days)?.price;
        return value ? { 会員: value } : {};
      });
      if (!soloDays) return { reason: 'unreadable-days' };
      soloPlans.push({ name: '30分', hours: band.range, last_entry: '', days: soloDays });
    }
  }
  if (flats.solo.size > 1 || flats.general.size > 1) return { reason: 'flat-varies' };
  const [flatSolo] = flats.solo;
  const [flatGeneral] = flats.general;
  const flat = flatSolo !== undefined || flatGeneral !== undefined ? { solo: flatSolo ?? null, general: flatGeneral ?? null } : null;
  // 別のページの 1 人料金の表があればそれを使う。読めなければ 1 人の料金は不確かなものとして扱う
  let soloTableUnread = false;
  if (soloPages.length) {
    const table = readTable(soloPages[0]);
    const list = table.reason ? null : soloFromTable(table, plans);
    if (list) soloPlans = list;
    else {
      soloPlans = [];
      soloTableUnread = true;
    }
  }
  const table = (list) => [{ band: '30分', customers: CUSTOMERS, plans: list, note: '' }];
  return {
    regular: table(plans),
    solo: soloPlans.length ? table(soloPlans) : [],
    flat: soloPlans.length ? null : flat,
    soloDiffers: (soloDiffers || soloTableUnread) && !soloPlans.length && !flat,
    minStay: /ご利用は1時間から/.test(whole) ? 60 : 0,
    alerts: [...new Set(whole.match(ALERT_WORDS) ?? [])],
  };
}
