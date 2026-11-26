// 歌広場の店のページから取った料金表（source/utahiro_prices.json）を engine.js の regular / week 形式にする。
// 見出し・時間帯・曜日・営業時間・告知は決まった形だけを読み、1 つでも知らない形があれば店ごと配らない
// （アプリは「料金表を見る」のまま）。読む形は tests/utahiro_conversion.test.mjs に並べてある。
//
// - 使うのは「ソフトドリンク飲み放題」（ドリンクバー付）の表だけ。アルコール飲み放題は読まない。
// - 区分は「ウタ学」（アプリ会員の学割）を学生会員、「一般」を一般にする。ウタ学の会員限定プランは画像なので読まない。
// - 「※2名様より受付」の列は出さない（engine に人数の条件がなく、1 人にも出してしまう）。
// - 1 人の料金は表にないので solo は作らない（engine が「1 人の料金は記載がない」と添える）。
// - 告知（p.db-infotext）に日付のある期間が、変換する日から 21 日先までと重なる店は出さない。アプリは 21 日より
//   古い配信ファイルの金額を使わないので、配った金額が告知の期間に使われることはない。

const DAY = 1440;
const WEEKDAYS = ['日', '月', '火', '水', '木', '金', '土'];
const CUSTOMERS = ['学生会員', '一般'];
const CUSTOMER = new Map([['ウタ学', '学生会員'], ['一般', '一般']]);
// 列見出し → engine の曜日ラベル。この 6 つだけを読む（engine の「平日」は、もう一方の列に当たらない日）
const DAY_HEADERS = new Map([
  ['月~金(祝日を除く)', '平日'], ['土・日・祝日', '土日祝'],
  ['日~木(祝前日を除く)', '平日'], ['金・土・祝前日', '金土祝前'],
  ['月~木(祝日・祝前日を除く)', '平日'], ['金・土・日・祝日・祝前日', '金土日祝祝前'],
]);
const PAIRS = new Set(['平日|土日祝', '平日|金土祝前', '平日|金土日祝祝前']);
const GROUP_ONLY = /^※\d名様より受付$/;
const BAND = /^(OPEN|\d{1,2}:\d{2})~(CLOSE|翌?\d{1,2}:\d{2})$/;
const RANGE = /^(\d{1,2}):(\d{2})~(翌)?(\d{1,2}):(\d{2})$/;
const PRICE = /^(?:\d{1,3}(?:,\d{3})+|\d{1,5})$/;
const SOFT = 'ソフトドリンク飲み放題';
const COURSES = new Set([SOFT, 'アルコール飲み放題']);
const IMAGE_BAND = 'ウタ学 アプリ会員限定プラン';
const SLOT_TITLE = 'お一人様30分料金';
const FREE_TITLE = 'お一人様フリータイム料金';
const DRINK = 'ドリンクバー付';
// 告知。年月日と曜日のある期間（「～まで」「の期間中は」）と、終わりのない「から」の 2 つの形だけ
const DATE = '(\\d{4})年(\\d{1,2})月(\\d{1,2})日\\(([月火水木金土日])\\)';
const CHANGE = '(?:一部料金|料金|オープン時間|営業時間|閉店時間)を[^。]{0,30}変更(?:いたします|させていただきます)。'
  + '(?:詳しくは店舗まで直接お問い合わせをお願いいたします。)?';
const NOTICE_RANGE = new RegExp(`^${DATE}~${DATE}(?:まで、|の期間中は)${CHANGE}$`);
const NOTICE_FROM = new RegExp(`^${DATE}から${CHANGE}$`);
// 配った金額をアプリが使う日数（app.js は 21 日より古い配信ファイルを使わない）
export const NOTICE_HORIZON_DAYS = 21;

const norm = (text) => String(text ?? '').normalize('NFKC').replace(/[～〜]/g, '~')
  .replace(/[ \t]+/g, ' ').trim();
const lines = (text) => String(text ?? '').split('\n').map(norm).filter(Boolean);
const clockText = (minutes) => `${Math.floor((minutes % DAY) / 60)}:${String(minutes % 60).padStart(2, '0')}`;

// 曜日・祝日・祝前日の組み合わせ。engine の曜日ラベルと営業時間はこの 28 通りで確かめる
const CONTEXTS = WEEKDAYS.flatMap((_, w) => [false, true].flatMap((holiday) =>
  [false, true].map((pre) => ({ w, holiday, pre }))));

function hitsToken(token, ctx) {
  if (token === '祝') return ctx.holiday;
  if (token === '祝前') return ctx.pre;
  return WEEKDAYS[ctx.w] === token;
}

const tokensOf = (label) => label.match(/祝前|[月火水木金土日祝]/g) ?? [];

// engine の pickDayRow と同じ選び方。「平日」はほかの行に当たらない日
function rowFor(days, ctx) {
  const other = days.find((row) => row.day !== '平日' && tokensOf(row.day).some((t) => hitsToken(t, ctx)));
  return other ?? days.find((row) => row.day === '平日') ?? null;
}

function parseRange(text) {
  const match = RANGE.exec(text);
  if (!match) return null;
  const [hour, minute, next, endHour, endMinute] = [match[1], match[2], match[3], match[4], match[5]];
  if (Number(minute) >= 60 || Number(endMinute) >= 60 || Number(hour) >= 24 || Number(endHour) > 24) return null;
  const open = Number(hour) * 60 + Number(minute);
  let close = Number(endHour) * 60 + Number(endMinute);
  if (next || close <= open) close += DAY;
  if (close <= open || close - open > DAY) return null;
  return [open, close];
}

// 「金・土・祝前日」「土・日・祝」「金祝前」「日祝」「土」を曜日の集まりにする
function daySet(label) {
  const text = label.replace(/祝前日/g, '祝前').replace(/祝日/g, '祝').replace(/・/g, '');
  const tokens = tokensOf(text);
  if (!tokens.length || tokens.join('') !== text || new Set(tokens).size !== tokens.length) return null;
  return tokens;
}

// 営業時間の欄を読む。読める形は次の 4 つだけ（ほかは null）
//   「11:00～翌5:00」、「24時間営業」
//   「11:00～23:00<br>金・土・祝前日：11:00～翌5:00」（1 行目が基本、2 行目からが曜日ごとの例外）
//   「午前11:00～翌5:00 (金祝前11：00～翌6：00/土：午前10:00～翌6:00/日祝：午前10:00～翌5:00)」
export function parseUtahiroHours(text) {
  const all = lines(text).map((line) => line.replace(/午前/g, ''));
  if (!all.length || all.some((line) => /午後/.test(line))) return null;
  if (all.length === 1 && all[0] === '24時間営業') {
    const whole = [0, DAY];
    return { always: true, week: Object.fromEntries([...WEEKDAYS, '祝前', '日祝'].map((key) => [key, whole])),
      wide: () => whole };
  }
  let base = all[0];
  let rest = all.slice(1);
  const bracket = /^(\S+) ?\((.+)\)$/.exec(base);
  if (bracket && all.length === 1) {
    base = bracket[1];
    rest = bracket[2].split('/');
  }
  const baseRange = parseRange(base);
  if (!baseRange) return null;
  const rules = [];
  for (const item of rest) {
    const match = /^([^\d:]+?):?(\d{1,2}:\d{2}~翌?\d{1,2}:\d{2})$/.exec(norm(item));
    const days = match && daySet(match[1]);
    const range = match && parseRange(match[2]);
    if (!days || !range) return null;
    rules.push({ days, range });
  }
  // その日に当たる例外がなければ基本。2 つ以上当たる日（祝前日の日曜など）はどちらか分からないので、
  // 狭い方（遅い開店・早い閉店）を使い、広い方は表とのつじつまの確認に使う
  const candidates = (ctx) => {
    const hits = rules.filter((rule) => rule.days.some((token) => hitsToken(token, ctx)));
    return hits.length ? hits.map((rule) => rule.range) : [baseRange];
  };
  const narrow = (ranges) => [Math.max(...ranges.map((r) => r[0])), Math.min(...ranges.map((r) => r[1]))];
  const wide = (ctx) => {
    const ranges = candidates(ctx);
    return [Math.min(...ranges.map((r) => r[0])), Math.max(...ranges.map((r) => r[1]))];
  };
  const week = {};
  WEEKDAYS.forEach((name, w) => { week[name] = narrow(candidates({ w, holiday: false, pre: false })); });
  // engine は祝日なら「日祝」、祝前日なら「祝前」を曜日より先に引くので、当たりうる日すべての狭い方にする
  week.祝前 = narrow(CONTEXTS.filter((c) => !c.holiday && c.pre).flatMap(candidates));
  week.日祝 = narrow(CONTEXTS.filter((c) => c.holiday).flatMap(candidates));
  if (Object.values(week).some(([open, close]) => close <= open)) return null;
  return { always: false, week, wide };
}

function dayNumber(year, month, day, weekday) {
  const time = Date.UTC(year, month - 1, day);
  const date = new Date(time);
  if (date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) return null;
  if (weekday && WEEKDAYS[date.getUTCDay()] !== weekday) return null; // 曜日が日付と合わない告知は読まない
  return time / 864e5;
}

export function dayOf(text) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text ?? '');
  return match ? dayNumber(Number(match[1]), Number(match[2]), Number(match[3])) : null;
}

// 告知を期間にする。読めない告知が 1 つでもあれば null（その店は出さない）
export function noticePeriods(notices) {
  if (!Array.isArray(notices)) return null;
  const periods = [];
  for (const raw of notices) {
    if (typeof raw !== 'string') return null;
    const text = norm(raw).replace(/^・\s*/, '');
    if (!text) continue;
    let match = NOTICE_RANGE.exec(text);
    if (match) {
      const from = dayNumber(Number(match[1]), Number(match[2]), Number(match[3]), match[4]);
      const to = dayNumber(Number(match[5]), Number(match[6]), Number(match[7]), match[8]);
      if (from === null || to === null || to < from) return null;
      periods.push({ from, to, price: /料金を/.test(text) });
      continue;
    }
    match = NOTICE_FROM.exec(text);
    if (match) {
      const from = dayNumber(Number(match[1]), Number(match[2]), Number(match[3]), match[4]);
      if (from === null) return null;
      periods.push({ from, to: Infinity, price: /料金を/.test(text) }); // 終わりの日付がない変更は、出さない側に倒す
      continue;
    }
    return null;
  }
  return periods;
}

// 期間が、変換する日から 21 日先までの入店と重なるか。前の日の夜からの滞在と、最後の日の夜から翌朝までの滞在も含める
export function overlapsHorizon(period, today) {
  return period.from <= today + NOTICE_HORIZON_DAYS + 1 && period.to >= today - 1;
}

function readHeader(cells) {
  if (!Array.isArray(cells) || cells.length !== 3 || norm(cells[0]) !== '') return null;
  const columns = [];
  for (const cell of cells.slice(1)) {
    const [head, ...notes] = lines(cell);
    const day = DAY_HEADERS.get(head);
    if (!day || notes.length > 1 || (notes.length && !GROUP_ONLY.test(notes[0]))) return null;
    columns.push({ day, groupOnly: notes.length === 1 });
  }
  return PAIRS.has(columns.map((column) => column.day).join('|')) ? columns : null;
}

function readCell(text, slot) {
  const value = norm(text);
  if (value === '-') return '-'; // その曜日はこのプランがない
  if (!PRICE.test(value)) return null;
  const yen = Number(value.replace(/,/g, ''));
  const ok = slot ? yen >= 50 && yen < 2000 : yen >= 100 && yen < 10000;
  return ok ? yen : null;
}

// 表の行を曜日ごとの金額にする。人数の条件がある列と「－」の列は金額なし（その曜日はプランを出さない）
function readDays(table, columns, slot) {
  const values = new Map();
  for (const row of table.rows ?? []) {
    if (!Array.isArray(row) || row.length !== 3) return null;
    const customer = CUSTOMER.get(norm(row[0]));
    if (!customer || values.has(customer)) return null;
    const cells = row.slice(1).map((cell) => readCell(cell, slot));
    if (cells.some((cell) => cell === null)) return null;
    values.set(customer, cells);
  }
  if (!values.has('一般') || (slot && !values.has('学生会員'))) return null;
  const days = [];
  for (const [index, column] of columns.entries()) {
    const dashes = [...values.values()].map((cells) => cells[index] === '-');
    // 一般だけ「－」で学生会員に金額がある、のような列はどちらの意味か分からない
    if (dashes.some(Boolean) && !dashes.every(Boolean)) return null;
    const prices = column.groupOnly || dashes[0] ? {}
      : Object.fromEntries([...values].map(([customer, cells]) => [customer, cells[index]]));
    days.push({ day: column.day, prices });
  }
  return days;
}

// フリータイムの見出し。列ごとの時間帯を返す。読める形は次の 3 つだけ
//   「お一人様フリータイム料金<br>11:00~20:00」
//   「…<br>11:00~20:00<br>土日祝10:00～20:00」（土日祝の列だけ時刻が違う）
//   「…<br>18:00～22:00<br>(金・土・祝前日 18:00～翌5:00)」（金土祝前の列だけ時刻が違う）
function freeRanges(title, columns) {
  const [head, base, extra, ...rest] = lines(title);
  if (head !== FREE_TITLE || !parseRange(base ?? '') || rest.length) return null;
  if (extra === undefined) return columns.map(() => base);
  let match = /^土日祝(\d{1,2}:\d{2}~翌?\d{1,2}:\d{2})$/.exec(extra);
  if (match && columns[1].day === '土日祝' && parseRange(match[1])) return [base, match[1]];
  match = /^\(金・土・祝前日 (\d{1,2}:\d{2}~翌?\d{1,2}:\d{2})\)$/.exec(extra);
  if (match && columns[1].day === '金土祝前' && parseRange(match[1])) return [base, match[1]];
  return null;
}

const hasPrices = (row) => Boolean(row && Object.keys(row.prices).length);

function readBand(band, index, labels, plans) {
  const soft = band.courses.filter((course) => norm(course.name) === SOFT);
  if (band.courses.some((course) => !COURSES.has(norm(course.name)))) return 'unknown-course';
  if (soft.length !== 1) return 'no-soft-course';
  const [from, to] = labels[index];
  let slots = 0;
  for (const table of soft[0].tables ?? []) {
    const columns = readHeader(table.header);
    if (!columns) return 'day-header';
    const title = norm(table.title);
    if (title === SLOT_TITLE) {
      if (columns.some((column) => column.groupOnly)) return 'slot-group-only';
      const days = readDays(table, columns, true);
      if (!days) return 'slot-rows';
      if (!days.every(hasPrices)) return 'slot-dash';
      slots += 1;
      plans.slot.push({ name: '30分', hours: `${from}～${to}`, last_entry: '', days, drink: DRINK });
      continue;
    }
    const ranges = freeRanges(table.title, columns);
    if (!ranges) return 'free-title';
    const days = readDays(table, columns, false);
    if (!days) return 'free-rows';
    for (const range of new Set(ranges)) {
      const planDays = days.map((row, i) => (ranges[i] === range ? row : { day: row.day, prices: {} }));
      if (!planDays.some(hasPrices)) continue;
      const [start, end] = parseRange(range);
      plans.free.push({ name: 'フリータイム', hours: `${clockText(start)}～${clockText(end)}`, last_entry: '',
        days: planDays, drink: DRINK, range: [start, end] });
    }
  }
  return slots === 1 ? null : 'slot-count';
}

// 時間帯の帯（「OPEN~18:00」「18:00~CLOSE」、24時間営業の店は「8:00~18:00」「18:00~翌8:00」）を読む
function readBandLabels(bands, hours) {
  const parsed = bands.map((band) => BAND.exec(norm(band.label)));
  if (parsed.some((match) => !match)) return { reason: 'band-label' };
  const [[, a, mid], [, mid2, b]] = parsed;
  if (mid !== mid2 || mid === 'CLOSE') return { reason: 'band-gap' };
  const clock = /^(\d{1,2}):(\d{2})$/.exec(mid);
  if (!clock || Number(clock[1]) >= 24 || Number(clock[2]) >= 60) return { reason: 'band-label' };
  const middle = Number(clock[1]) * 60 + Number(clock[2]);
  if (hours.always) {
    // 24時間営業なのに OPEN/CLOSE の表（開店・閉店の時刻が決まらない）は読まない
    if (a === 'OPEN' || b === 'CLOSE') return { reason: '24h-open-close' };
    const first = parseRange(`${a}~${mid}`);
    const second = parseRange(`${mid}~${b}`);
    if (!first || !second || (first[0] + DAY) % DAY !== second[1] % DAY) return { reason: 'band-gap' };
    return { labels: [[a, mid], [mid, b.replace('翌', '')]] };
  }
  if (a !== 'OPEN' || b !== 'CLOSE') return { reason: 'band-not-open-close' };
  // 昼と夜の境目は、どの日も営業中であること
  for (const [open, close] of Object.values(hours.week)) {
    if (!(open < middle && middle < close) && !(open < middle + DAY && middle + DAY < close)) {
      return { reason: 'band-outside-hours' };
    }
  }
  return { labels: [['開店', mid], [mid, '閉店']] };
}

const inside = (range, [open, close]) => range[0] >= open && range[1] <= close;

// フリータイムの時刻が、その列の曜日の営業時間に収まること。同じ時刻のフリータイムが同じ曜日に 2 つ当たらないこと
function checkFree(plans, hours) {
  for (const ctx of CONTEXTS) {
    const seen = new Set();
    for (const plan of plans) {
      if (!hasPrices(rowFor(plan.days, ctx))) continue;
      if (!hours.always && !inside(plan.range, hours.wide(ctx))) return 'free-outside-hours';
      const key = plan.range.join('-');
      if (seen.has(key)) return 'free-overlap';
      seen.add(key);
    }
  }
  return null;
}

function checkTail(tail) {
  const text = norm((tail ?? []).join('\n'));
  if (!/お1人様税込料金です/.test(text)) return 'tax';
  if (/税別|税抜|別途|室料|ルームチャージ|入室料|サービス料|入会金/.test(text)) return 'extra-charge';
  return null;
}

// 1 店を変換する。告知の期間は convertUtahiroShops が確かめる
export function convertUtahiro(shop) {
  if (!shop || typeof shop !== 'object') return { reason: 'record' };
  if (shop.missing) return { reason: 'missing' };
  if (!Array.isArray(shop.bands) || !shop.bands.length) return { reason: 'no-table' };
  if (Array.isArray(shop.extras) ? shop.extras.length : true) return { reason: 'extras' };
  const tailIssue = checkTail(shop.tail);
  if (tailIssue) return { reason: tailIssue };
  const hours = parseUtahiroHours(shop.hours);
  if (!hours) return { reason: 'hours' };
  const priced = shop.bands.filter((band) => Array.isArray(band.courses) && band.courses.length);
  const others = shop.bands.filter((band) => !Array.isArray(band.courses) || !band.courses.length);
  if (others.some((band) => norm(band.label) !== IMAGE_BAND)) return { reason: 'unknown-band' };
  if (priced.length !== 2) return { reason: 'band-count' };
  const bandLabels = readBandLabels(priced, hours);
  if (bandLabels.reason) return { reason: bandLabels.reason };
  const plans = { slot: [], free: [] };
  for (const [index, band] of priced.entries()) {
    const reason = readBand(band, index, bandLabels.labels, plans);
    if (reason) return { reason };
  }
  const freeIssue = checkFree(plans.free, hours);
  if (freeIssue) return { reason: freeIssue };
  const regular = [{ band: '30分', customers: CUSTOMERS, plans: plans.slot, note: '' }];
  if (plans.free.length) {
    regular.push({ band: 'フリータイム', customers: CUSTOMERS,
      plans: plans.free.map(({ range, ...plan }) => plan), note: '' });
  }
  return { regular, week: hours.week };
}

// 全店を変換する。today は変換する日（JST の 'YYYY-MM-DD'）。
// 告知の期間がこの日から 21 日先までと重なる店は出さない。ほかの店で見つかった料金変更の期間も、その告知を見た日時より
// 前に確かめた店には当てる（3 日に 1 回しか取らないので、全店に出た告知にまだ気づいていないかもしれない）。
// 営業時間の変更は店ごとのことなので、ほかの店には当てない
export function convertUtahiroShops(records, today) {
  const day = dayOf(today);
  if (day === null) throw new Error(`utahiro: 変換する日が不正です (${today})`);
  const shops = {};
  const reasons = {};
  const parsed = {};
  const seenAt = new Map(); // 期間 → その告知のある店を確かめた日時の最小
  for (const [id, shop] of Object.entries(records ?? {})) {
    const periods = shop && typeof shop === 'object' ? noticePeriods(shop.notices) : null;
    parsed[id] = periods;
    for (const period of (periods ?? []).filter((item) => item.price)) {
      const key = `${period.from}-${period.to}`;
      const at = Number.isInteger(shop.checked_at) ? shop.checked_at : 0;
      const known = seenAt.get(key);
      if (!known || at < known.at) seenAt.set(key, { period, at });
    }
  }
  for (const [id, shop] of Object.entries(records ?? {})) {
    let reason;
    try {
      const periods = parsed[id];
      const checked = Number.isInteger(shop?.checked_at) ? shop.checked_at : 0;
      if (!shop || typeof shop !== 'object') reason = 'record';
      else if (periods === null) reason = 'notice-unknown';
      else if (periods.some((period) => overlapsHorizon(period, day))) reason = 'notice-period';
      else if ([...seenAt.values()].some(({ period, at }) => checked < at && overlapsHorizon(period, day))) {
        reason = 'notice-other-shop';
      } else {
        const converted = convertUtahiro(shop);
        if (converted.reason) reason = converted.reason;
        else shops[id] = converted;
      }
    } catch (error) {
      reason = `error: ${error?.message ?? error}`;
    }
    if (reason) (reasons[reason] ??= []).push(id);
  }
  return { shops, reasons };
}

// 変換する日。JST の今日。CONVERT_TODAY（YYYY-MM-DD）で差し替えられる（手で確かめるとき用）
export function todayJst(env = process.env, now = Date.now()) {
  if (env.CONVERT_TODAY) return env.CONVERT_TODAY;
  return new Date(now + 9 * 3600e3).toISOString().slice(0, 10);
}
