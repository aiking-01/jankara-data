// まねきねこの料金表 PDF から取り出した文字の並びを engine.js の regular / solo の形へ変換する。DOM に依存しない。
// 誤った金額を出さないことを最優先にする。表の形が少しでも想定と違えば店ごと読まず、公式の料金表へ案内する。
// 対象は 30 分料金だけ。フリータイム、パック、ZERO カラ、小中学生コース、まふは計算しない。
// PDF は複数ページのものが多く、1 ページ目は 2 名以上の料金で、1 人料金が別のページにある店がある。
// 文字は [文字列, 左, 上, 右, 下]。座標はページごとで、y は下向き。
// 料金表のほかに、PDF の全ページの文、ページごとの画像の量、店のページのお知らせも見る（update_maneki_prices.py が残す）。
// 特別料金・年末年始・料金改定などの告知の期間が今日以降まで続く店は読まない（アプリの日付選びには先の上限がない）。
// 期間が読めない告知、今日以降の日付がある文、特別営業時間の欄に文がある店も読まない。

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

// 帯の時刻の右にある説明。30 分料金に関わるかもしれない説明がある表は読まない。
// 帯の「最終受付」は無害ではない（受付の後の入店に金額を出してしまう）ので、ここでは通さず bandNote で読む
const HARMLESS_NOTE = /^(?:※?(?:[^※]*(?:フリー|FT)[^※]*|[^※]*混雑[^※]*|(?:お1人様・非会員様|おひとり様利用)は一般料金[^※]*)?|ワンドリンクorドリンクバー|※?(?:ワン)?ドリンクオーダー制)$/;
const CHANGES_PRICE = /30分|一律|追加|\+\d|\d+月\d+日|期間/;
// フリータイムの説明に見えても、帯の金額の使い方を変える注記（「日・祝は【深夜フリータイム】の料金で18時から入室可」など）
const CHANGES_USE = /料金|入室|適用/;
// 帯の最終受付。「※最終受付は翌朝4:00」「※最終受付は翌3:59まで」
const LAST_ENTRY = /^※?最終(?:受付|入室)(?:は|:)?(翌朝?)?(\d{1,2}):(\d{2})(?:まで)?$/;
const OTHER_PLAN_NAME = /フリー|FT|パック|まふ/;
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

function noteOf(tokens, pair) {
  const line = tokens.filter((token) => token.left > pair[1].right && token.top < pair[1].bottom && token.bottom > pair[1].top && !SECTION_HEAD.test(token.text) && !token.text.startsWith('≪'))
    .sort((a, b) => a.left - b.left).map((token) => token.text).join('');
  return line.replace(/\(フリー[^)]*\)/g, '').replace(/ワンドリンクorドリンクバー$/, '');
}

// 帯の注記に、フリータイムやパックではなく帯そのものの最終受付が書いてあるか
function mentionsLastEntry(note) {
  for (const segment of note.split('※')) {
    const text = segment.replace(/\([^()]*\)/g, (group) => (OTHER_PLAN_NAME.test(group) ? '' : group));
    for (const match of text.matchAll(/最終(?:受付|入室)/g)) {
      if (!OTHER_PLAN_NAME.test(text.slice(0, match.index))) return true;
    }
  }
  return false;
}

// 帯の注記を読む。{ allowed, lastEntry, reason }。帯の最終受付は読める形なら lastEntry にし、読めなければ帯ごと読まない。
// アプリ（engine.js）は 30 分料金の計算で last_entry を使わないので、最終受付が帯の終わりより前なら、
// 受付の後の入店にも金額を出してしまう。アプリが対応するまでは、そういう帯は読まない（理由 band-last-entry）
function bandNote(tokens, pair) {
  const note = noteOf(tokens, pair);
  const entry = LAST_ENTRY.exec(note);
  if (entry) {
    const clock = `${entry[1] ? '翌' : ''}${Number(entry[2])}:${entry[3]}`;
    const [from, to] = spanOf(pair[0].text, pair[1].text);
    const last = spanOf(pair[0].text, clock)[1];
    if (last <= from || last > to) return { allowed: false };
    return last < to ? { allowed: false, reason: 'band-last-entry' } : { allowed: true, lastEntry: clock };
  }
  if (mentionsLastEntry(note)) return { allowed: false };
  if (/フリー|FT/.test(note) && CHANGES_USE.test(note)) return { allowed: false };
  return { allowed: noteAllows(note, pair), lastEntry: '' };
}

function noteAllows(note, pair) {
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
  return (HARMLESS_NOTE.test(note) && !CHANGES_PRICE.test(note)) || (onlyHours(note) && notNarrower(note, pair));
}

// 注記の時刻が帯より狭ければ、その曜日は帯の一部で店が開いていないか 30 分料金を受け付けない。
// 「※月~金・祝前は13時OPEN」「※日~木・祝は5時CLOSE」「※平日は14:00~18:00」は読まない
const NOTE_TIME = /(翌?)(\d{1,2})(?::(\d{2})|時(?!間))/g;
const ONLY_OTHER_PLAN = /パックのみ|パックとなります|のみの受付|最終受付|受付時間/;
function notNarrower(note, pair) {
  // フリータイムの説明に混ざった上乗せ（「時間制+110円フリータイム…」）も金額を変える
  if (ONLY_OTHER_PLAN.test(note) || CHANGES_PRICE.test(note) || /円/.test(note)) return false;
  const text = note.replace(OTHER_PLANS, '');
  const [from, to] = spanOf(pair[0].text, pair[1].text);
  // 「18:00まで営業」は夜の帯がない日。帯の始まりと同じ時刻で終わるなら狭い
  const endOf = (clock) => (clockOf(clock) === from % 1440 && !clock.startsWith('翌') ? from : spanOf(pair[0].text, clock)[1]);
  const times = [...text.matchAll(NOTE_TIME)].map((match) => ({
    clock: `${match[1]}${match[2]}:${match[3] ?? '00'}`, index: match.index, end: match.index + match[0].length,
  }));
  for (let i = 0; i < times.length; i += 1) {
    const time = times[i];
    const next = times[i + 1];
    const before = text.slice(0, time.index);
    const after = text.slice(time.end);
    // 2 つ並んだ時刻は始まりと終わり。「18:0023:00」のように区切りが抜けることがある
    if (next && /^[~～]?$/.test(text.slice(time.end, next.index))) {
      if (clockOf(time.clock) > from % 1440 || endOf(next.clock) < to) return false;
      i += 1;
    } else if (/^(?:OPEN|[~～](?!\d)|より|から)/.test(after) && !/[~～]$/.test(before)) {
      if (clockOf(time.clock) > from % 1440) return false;
    } else if (endOf(time.clock) < to) return false;
  }
  return true;
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
  const note = bandNote(tokens, pair);
  if (!note.allowed) return { reason: note.reason ?? 'band-note' };
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
  return { from: pair[0].text, to: pair[1].text, range: rangeOf(pair), lastEntry: note.lastEntry, sections, plus };
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
    list.push({ name: '30分', hours: band.range, last_entry: band.lastEntry ?? '', days });
  }
  const hours = (items) => items.map((item) => item.hours).sort().join();
  return hours(list) === hours(plans) ? list : null;
}

// ---- 告知と記録の確認 ----

// 取り出しの版。update_maneki_prices.py の EXTRACT と同じ値。告知（全ページの文、ページごとの画像の量、
// 店のページのお知らせ）を残していない古い記録は、告知を確かめられないので読まない
export const EXTRACT_VERSION = 2;
const DAY_MS = 86400000;
// 料金や営業の期間を知らせる語。文字の切れ目を除いた文で探す。
// 告知の期間が今日以降まで続くなら、始まりがいつでも読まない（アプリの日付選びには先の上限がないので、
// 12/30 を選んだ利用者に通常料金を出さないため）。期間が読めない告知も読まない
const PERIOD_WORDS = /特別(?:料金|価格|期間|営業|日料金|室料)|年末|年始|正月|大晦日|元旦|元日|三が日|お盆|GW|ゴールデンウィーク|大型連休|繁忙|改定|改訂|値上げ|値下げ|(?:料金|価格|室料)を?変更|新料金|新価格|特定日|休業|臨時休|時短営業|営業時間の?変更|クリスマス(?:料金|価格|期間)/g;
// 時期の決まった語。近くの日付がこの月にかからなければ、その日付は告知の期間ではない（掲載日や別の催しの日付）
const SEASONS = [
  [/年末|年始|正月|大晦日|元旦|元日|三が日/, [12, 1]],
  [/お盆/, [7, 8]],
  [/GW|ゴールデンウィーク/, [4, 5]],
  [/クリスマス/, [12]],
];
// 全店に載る断り書き（「年末年始などの特別期間は料金が異なる場合がございます」など）。日付のない断り書きは告知とみなさない。
// 除くのはこの文の中の語だけで、同じ文節にほかの言い切りの告知があれば、そちらは告知として見る
const HEDGE_NAMES = '(?:ゴールデンウィーク|GW|夏休み期間|夏休み|春休み|冬休み|お盆|年末年始|大型連休|連休|土日祝日?|祝日|祝前日)';
const HEDGES = new RegExp([
  `(?:${HEDGE_NAMES}[・、]?)*(?:など|等)?の?(?:特別期間|繁忙期|特定日)は、?(?:料金|室料)が異なる場合が(?:ござい|あり)ます`,
  '上記料金表?および料金は予告なく変更(?:に|と)なる場合が(?:ござい|あり)ます',
].join('|'), 'g');
// 告知の語から日付を探す範囲（文字数）。PDF の文は描かれた順なので、遠くの日付は別の欄のものとみなす
const NEAR_BEFORE = 40;
const NEAR_AFTER = 60;
const DATE_ITEM = new RegExp([
  /(?:(20\d{2})年)?(\d{1,2})月(\d{1,2})日/.source, // 12月29日、2026年12月29日
  /(?<![\d/.])(?:(20\d{2})[/.])?(\d{1,2})\/(\d{1,2})(?![\d/])/.source, // 12/29、2026/12/29
  /(20\d{2})\.(\d{1,2})\.(\d{1,2})(?![\d.])/.source, // 2026.12.29
  /(?<=[~〜・、,])(\d{1,2})日/.source, // 12月29日・30日、11月2日~4日 の後ろの日
  /(?:(20\d{2})年)?(\d{1,2})月(?!\d)(?![~〜](?!\d))/.source, // 12月より、1月まで、10月~3月（「月~金」の曜日は除く）
  /(?<=[~〜・、,])(\d{1,2})(?![\d:/.,月日時分名人円%])/.source, // 12/30・31、12/29~31 の後ろの日（直前の日付に続くときだけ）
].join('|'), 'g');
// 日だけの項目が直前の日付に続いているか（「12/30・31」「12月29日(月)~31日」）
const FOLLOWS = /^(?:\([^()]{0,6}\))?[~〜・、,]$/;
// 掲載日や「〜時点」の日付。告知の期間ではない（「2026年11月1日現在」「11/1更新」「2026/10/25時点のものです」）
const AS_OF = /^(?:\([^()]{0,6}\))?(?:現在|時点|更新|掲載|発行|作成|付け?)/;
const RANGE = /^(?:[~〜\-ー－]|から|より)$/;
// 店のページの特別営業時間の欄（update_maneki_prices.py の store_notices が kind を付ける）
const SPECIAL_HOURS = 'special-hours';
// 画像が料金表のページを覆っていないか、表でないページが画像の告知や画像にした料金表でないか。
// look は update_maneki_prices.py が残すページの見た目の指紋（64 ビット）。同じ案内の画像なら数ビットしか違わない
const VISUAL_AREA = 0.15;
const LOOK_DISTANCE = 4; // 同じ画像とみなす違い
const TABLE_LIKE = 12; // どこかの店の料金表のページとこれ以下の違いなら、画像にした料金表とみなす
const COMMON_SHARE = 0.05; // 画像のページがある店のうち、これだけの店（少なくとも COMMON_MIN 店）に同じ画像があれば全店共通の案内
const COMMON_MIN = 10;
// 共通の案内として数えるのは、その店にこの日数より前から載っている画像だけ（page.since）。
// 本部が新しく全店に入れた画像（年末年始の特別料金の案内など）は、この間は共通とみなさずに読まない
const SETTLED_DAYS = 30;

const dayNumber = (year, month, day) => Date.UTC(year, month - 1, day) / DAY_MS;
const lastDayOf = (year, month) => new Date(Date.UTC(year, month, 0)).getUTCDate();
// UNIX 時刻（秒）を JST の日の番号にする
const dayOfSeconds = (seconds) => Math.floor((seconds * 1000 + 9 * 3600 * 1000) / DAY_MS);

// 変換する日（JST の今日）。確かめるときは引数か CONVERT_DATE=YYYY-MM-DD で差し替える
export function todayNumber(given) {
  const env = typeof process !== 'undefined' ? (process.env.CONVERT_DATE || process.env.CONVERT_TODAY) : undefined;
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(given ?? env ?? '');
  if (match) return dayNumber(Number(match[1]), Number(match[2]), Number(match[3]));
  return Math.floor((Date.now() + 9 * 3600 * 1000) / DAY_MS);
}

// 年のない日付は今日に最も近い年とみなす
function nearestYear(month, day, today) {
  const year = new Date(today * DAY_MS).getUTCFullYear();
  return [year - 1, year, year + 1].reduce((best, candidate) => (
    Math.abs(dayNumber(candidate, month, day) - today) < Math.abs(dayNumber(best, month, day) - today) ? candidate : best));
}

// 告知の文を比べやすい形にする。PDF の文は語の途中に空白が入るので空白を除くが、
// 数字と数字の間の空白は区切りとして残す（「22:00 1/1」を「22:001/1」にして 1/1 を読み落とさない）
const noticeText = (raw) => (raw ?? '').normalize('NFKC').replace(/(\d)\s+(?=\d)/g, '$1_').replace(/\s+/g, '');

// 文の中の日付を [{ index, end, month, day?, year? }] で返す。日だけの項目は直前の月を使う
function dateItems(text) {
  const items = [];
  for (const match of text.matchAll(DATE_ITEM)) {
    const [, y1, m1, d1, y2, m2, d2, y3, m3, d3, d4, y5, m5, d6] = match;
    const base = { index: match.index, end: match.index + match[0].length };
    const last = items.at(-1);
    let item = null;
    if (m1) item = { ...base, year: y1, month: m1, day: d1 };
    else if (m2) item = { ...base, year: y2, month: m2, day: d2 };
    else if (m3) item = { ...base, year: y3, month: m3, day: d3 };
    else if (d4 && last) item = { ...base, year: last.year, month: last.month, day: d4, follows: true };
    else if (m5) item = { ...base, year: y5, month: m5, day: null };
    else if (d6 && last && last.day !== null && FOLLOWS.test(text.slice(last.end, match.index))) {
      item = { ...base, year: last.year, month: last.month, day: d6, follows: true };
    }
    if (!item || AS_OF.test(text.slice(item.end))) continue;
    item.year = item.year ? Number(item.year) : null;
    item.month = Number(item.month);
    item.day = item.day === null ? null : Number(item.day);
    if (item.month < 1 || item.month > 12 || (item.day !== null && (item.day < 1 || item.day > 31))) continue;
    items.push(item);
  }
  return items;
}

const stripDays = (text) => text.replace(/\([^()]{0,6}\)/g, '');

// 日付の項目を [{ start, end, index, last }]（start と end は日の番号。開いた端は ±Infinity。index と last は文の中の位置）にする
function spansOf(text, items, today) {
  const spans = [];
  const at = (item, year, first) => {
    if (item.day !== null) return dayNumber(year, item.month, item.day);
    return dayNumber(year, item.month, first ? 1 : lastDayOf(year, item.month));
  };
  for (let i = 0; i < items.length; i += 1) {
    const item = items[i];
    const next = items[i + 1];
    const year = item.year ?? nearestYear(item.month, item.day ?? 15, today);
    // 「12月29日(月)~1月3日(土)」「11月2日~4日」
    if (next && RANGE.test(stripDays(text.slice(item.end, next.index)))) {
      const start = at(item, year, true);
      let nextYear = next.year ?? year;
      if (!next.year && at(next, nextYear, false) < start) nextYear += 1;
      spans.push({ start, end: at(next, nextYear, false), index: item.index, last: next.end });
      i += 1;
      continue;
    }
    const after = stripDays(text.slice(item.end, item.end + 10));
    const before = text.slice(Math.max(0, item.index - 2), item.index);
    const place = { index: item.index, last: item.end };
    if (/^(?:[~〜]|から|より|以降)/.test(after)) spans.push({ start: at(item, year, true), end: Infinity, ...place });
    else if (/^まで/.test(after) || /[~〜]$/.test(before)) spans.push({ start: -Infinity, end: at(item, year, false), ...place });
    else spans.push({ start: at(item, year, true), end: at(item, year, false), ...place });
  }
  return spans;
}

// 期間がその月にかかるか。開いた期間や 1 年を超える期間はかかるとみなす
function inSeason(span, months) {
  if (!Number.isFinite(span.start) || !Number.isFinite(span.end) || span.end - span.start > 366) return true;
  for (let day = span.start; day <= span.end; day += 1) {
    if (months.includes(new Date(day * DAY_MS).getUTCMonth() + 1)) return true;
  }
  return false;
}

// 今日以降の日付を含むか（「12/31は 22:00 閉店」「~12/25まで」「12/1より」）。過ぎた日から始まる開いた期間（「10/1より」）は含まない
const upcoming = (span, today) => Math.max(...[span.start, span.end].filter(Number.isFinite)) >= today;

// 告知の文から、読まない理由を返す。
// 'notice': 特別料金・年末年始・料金改定などの語があり、その期間が今日以降まで続く
// 'notice-undated': その語の近くに期間の日付がない（断り書きの中の語は除く）
// 'notice-date': 語がなくても、今日以降の日付がある（営業時間の変更、休業、催しなど。料金や営業が変わるかもしれない）
export function noticeReason(texts, today) {
  for (const raw of texts) {
    for (const clause of noticeText(raw).split(/[。※◆■●★☆]/)) {
      const spans = spansOf(clause, dateItems(clause), today);
      const hedges = [...clause.matchAll(HEDGES)].map((match) => [match.index, match.index + match[0].length]);
      for (const word of clause.matchAll(PERIOD_WORDS)) {
        const from = word.index;
        const to = word.index + word[0].length;
        if (hedges.some(([start, end]) => start <= from && to <= end)) continue;
        const season = SEASONS.find(([pattern]) => pattern.test(word[0]))?.[1];
        const near = spans.filter((span) => span.last >= from - NEAR_BEFORE && span.index <= to + NEAR_AFTER
          && (!season || inSeason(span, season)));
        if (!near.length) return 'notice-undated';
        if (near.some((span) => span.end >= today)) return 'notice';
      }
      if (spans.some((span) => upcoming(span, today))) return 'notice-date';
    }
  }
  return null;
}

// 特別営業時間の欄はふだんは空なので、文があれば営業や料金がふだんと違う。書かれた日付がすべて過ぎたと分かるまで読まない
// （日付がなければ読まない）。「12/31は 22:00 閉店」「1月1日はお休みです」
export function specialHoursOver(text, today) {
  const clean = noticeText(text);
  const spans = spansOf(clean, dateItems(clean), today);
  return spans.length > 0 && spans.every((span) => Number.isFinite(span.end) && span.end < today);
}

// 表でないページのうち、画像や文字を描いた図形が中心のもの（update_maneki_prices.py の visual と同じ条件）
const isVisual = (page) => !page.table
  && ((page.image_area ?? 0) >= VISUAL_AREA || ((page.chars ?? 0) < 20 && ((page.paths ?? 0) >= 100 || (page.images ?? 0) >= 10)));

const bitCount = (value) => {
  let count = 0;
  for (let rest = value >>> 0; rest; rest &= rest - 1) count += 1;
  return count;
};
const lookBits = (look) => [parseInt(look.slice(0, 8), 16), parseInt(look.slice(8), 16)];
const lookDistance = (a, b) => bitCount(a[0] ^ b[0]) + bitCount(a[1] ^ b[1]);

// 多くの店の PDF に、前から同じ見た目で入っている画像のページ（全店共通の飲み放題の案内など）の look。
// 店ごとの告知の画像は 1 店か数店にしかない。料金表に似た画像は、多くの店にあっても共通の案内とみなさない
// （年末年始の料金表を画像で足した店が多くても通さない）。数えるのは SETTLED_DAYS より前からその店にある画像だけ
// （本部が新しく全店に入れた画像の告知を、共通の案内として通さない）
export function commonLooks(shops, today = todayNumber()) {
  const visuals = [];
  const tables = [];
  const valid = (look) => /^[0-9a-f]{16}$/.test(look ?? '');
  for (const [id, shop] of Object.entries(shops ?? {})) {
    if (shop?.extract !== EXTRACT_VERSION) continue;
    for (const page of shop.pages ?? []) {
      if (!valid(page.look)) continue;
      if (page.table) tables.push(lookBits(page.look));
      else if (isVisual(page)) {
        const settled = Number.isInteger(page.since) && page.since > 0 && dayOfSeconds(page.since) <= today - SETTLED_DAYS;
        visuals.push({ id, look: page.look, bits: lookBits(page.look), settled });
      }
    }
  }
  const owners = new Set(visuals.map((item) => item.id)).size;
  const needed = Math.max(COMMON_MIN, Math.ceil(owners * COMMON_SHARE));
  const settled = visuals.filter((item) => item.settled);
  const common = new Set();
  for (const item of visuals) {
    if (common.has(item.look) || tables.some((bits) => lookDistance(bits, item.bits) <= TABLE_LIKE)) continue;
    const others = new Set(settled.filter((other) => other.id !== item.id && lookDistance(item.bits, other.bits) <= LOOK_DISTANCE).map((other) => other.id));
    if (others.size >= needed) common.add(item.look);
  }
  return common;
}

// 記録から読まない理由を返す。表を読む前に確かめる
function recordReason(info, options) {
  if (!info || info.extract !== EXTRACT_VERSION) return 'old-extract';
  if (info.status) return String(info.status);
  // 料金表の PDF が 2 つ以上あると、どれが通常の料金か分からない（期間限定の PDF を通常料金として読まない）
  if (info.pdfs !== 1) return info.pdfs ? 'several-pdfs' : 'no-pdf';
  const today = todayNumber(options.today);
  const notices = [...(info.notices ?? []), ...(info.news ?? [])];
  if (notices.some((notice) => notice?.cut)) return 'notice-cut';
  if (notices.some((notice) => notice?.kind === SPECIAL_HOURS && !specialHoursOver(notice.text, today))) return 'special-hours';
  const notice = noticeReason(notices.map((item) => item?.text), today);
  if (notice) return notice;
  for (const page of info.pages ?? []) {
    if (page.table && (page.image_area ?? 0) >= VISUAL_AREA) return 'image-on-table';
    if (isVisual(page) && !options.commonLooks?.has(page.look)) return 'image-page';
  }
  return null;
}

// convert.mjs から使う。全店の記録から共通の画像を数えてから、店ごとに変換する関数を返す
export function manekiConverter(shops, options = {}) {
  const common = commonLooks(shops, todayNumber(options.today));
  return (shop) => convertManeki(shop?.tables, shop, { ...options, commonLooks: common });
}

// 店の記録（update_maneki_prices.py が残したもの）から { regular, solo, flat, minStay } か { reason } を返す。
// info を渡さなければ告知を確かめられないので読まない
export function convertManeki(tables, info, options = {}) {
  const reason = recordReason(info, options);
  if (reason) return { reason };
  return convertTables(tables);
}

// PDF のページごとの文字の並びから { regular, solo, flat, minStay } か { reason } を返す
function convertTables(tables) {
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
    plans.push({ name: '30分', hours: band.range, last_entry: band.lastEntry ?? '', days, drink });
    const soloCells = solo ?? (soloIsGeneral ? general : null);
    if (soloCells) {
      const soloDays = daysRows(members, (cell) => {
        const value = soloCells.find((item) => item.days === cell.days)?.price;
        return value ? { 会員: value } : {};
      });
      if (!soloDays) return { reason: 'unreadable-days' };
      soloPlans.push({ name: '30分', hours: band.range, last_entry: band.lastEntry ?? '', days: soloDays });
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
