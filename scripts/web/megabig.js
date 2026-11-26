// メガビッグ公式料金表（update_megabig.py が保存した sections）を料金ナビの regular / solo 形式に変換する。DOM に依存しない。
// 誤った金額を出さないことを最優先にする。確実に読めない店・表・曜日は出さず、出さなかった理由を dropped に残す
// （convert.mjs が警告に出す。警告を見る運用は前提にしない）。
// - 最終受付は「最終受付16:00」「最終受付（16:00）」「受付時間（OPEN-11:00）」を読む。時刻が 2 つ以上あるもの、
//   読めないもの、曜日で変わるものはそのプランを出さない。
// - 列の値がすべて「-」の曜日は提供なし（料金なしの行）にする。一部だけ「-」の表は出さない。
// - 備考に曜日の語が残るプラン（「土・日・祝は19:00まで」など）は出さない。曜日は語として読み、「料金」の「金」は曜日にしない。
// - 備考の曜日と列の曜日が食い違う表は、同じ曜日の列を持つ表ごと出さない（「金・土・祝」の列に「金・土・祝前は…」の備考）。
// - 1 人の金額は、表ごとの追加料金の文と、店全体の「2名様以上の料金」の文から決める。決められない曜日は、
//   30 分は 1 人の行を空にし（アプリは注意書きを付ける）、フリータイム・パックはその曜日の料金を出さない
//   （アプリはフリータイム・パックに 1 人の注意書きを出せず、2 名以上の金額を 1 人に出してしまうため。ビッグエコーと同じ扱い）。
//   表の外の文に 1 人の料金の文があれば、店全体で 1 人の金額を決められないものとして扱う。
// - 期間料金の告知（年末年始・夏季・「表示料金は…まで」など）は、文ごとに読む。今日以降と重なる告知は、先の告知でも店ごと出さない
//   （アプリは先の日付をいくらでも選べ、一覧には注意書きが出ないため）。終わりの日が読めない告知、「…まで」だけの告知、
//   日付のない告知の語、月や日だけの日付も出さない。過ぎた告知は、日付のすぐ後の期間の語（「～9月6日(日)は夏季料金」）のほかに
//   期間の語がなく、曜日が今日にいちばん近い年の暦に合うときだけ無視する（去年の曜日のまま出した告知を去年のものと読まない）。
//   料金の PDF や画像へのリンク、読めない表（取得で捨てた表や読めないセル）があれば店ごと出さない。
import { dayLabels, parseDays, parseTimeRange } from './bigecho.js';

const CUSTOMERS = ['学生会員', '学生', '会員', 'シニア', '一般'];
const DAY_MS = 864e5;
const WEEK = ['日', '月', '火', '水', '木', '金', '土'];
const norm = (text) => (text ?? '').normalize('NFKC').replace(/\s+/g, '');
const soft = (text) => (text ?? '').normalize('NFKC').replace(/\s+/g, ' ').trim();

// ---- 変換する日 ----

// JST の今日を日番号（1970-01-01 からの日数）で返す。テストでは引数か CONVERT_TODAY=YYYY-MM-DD で差し替える
export function jstDay(value) {
  const given = value ?? (typeof process === 'undefined' ? undefined : process.env.CONVERT_TODAY);
  const match = typeof given === 'string' ? /^(\d{4})-(\d{2})-(\d{2})$/.exec(given.trim()) : null;
  if (match) return Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])) / DAY_MS;
  if (typeof given === 'number') return given;
  const now = given instanceof Date ? given.getTime() : Date.now();
  return Math.floor((now + 9 * 3600e3) / DAY_MS);
}

// ---- 営業時間 ----

function clock(text) {
  const [hour, minute] = text.split(':').map(Number);
  return hour * 60 + minute;
}

// 公式の「月～木 11:00～5:00」「全日 10時～翌5時」を engine の week 形式にする。
export function parseMegabigHours(text) {
  const normalized = norm(text)
    .replace(/(\d{1,2})時(?:(\d{1,2})分)?/g, (_, hour, minute) =>
      `${hour}:${String(minute ?? '00').padStart(2, '0')}`)
    .replace(/翌/g, '');
  const ranges = [...normalized.matchAll(/(\d{1,2}:\d{2})[~〜～-](\d{1,2}:\d{2})/g)];
  if (!ranges.length) return null;
  const week = {};
  let cursor = 0;
  for (const match of ranges) {
    const prefix = normalized.slice(cursor, match.index);
    const labelMatch = /(?:全日|[月火水木金土日祝前曜祭・,、~〜～/-]+)$/.exec(prefix);
    const label = labelMatch?.[0] || (ranges.length === 1 ? '全日' : '');
    const days = parseDays(label);
    if (!days) return null;
    const open = clock(match[1]);
    let close = clock(match[2]);
    while (close <= open) close += 1440;
    for (const day of days) {
      const key = day === '祝' ? '日祝' : day;
      if (week[key] && (week[key][0] !== open || week[key][1] !== close)) return null;
      week[key] = [open, close];
      if (day === '日') week['日祝'] ??= [open, close];
    }
    cursor = match.index + match[0].length;
  }
  return Object.keys(week).length ? week : null;
}

// ---- 曜日の語 ----

const DAY_TOKEN = '(?:[月火水木金土日](?:曜日?)?|祝前日?|祝祭?日?|平日|休日|週末|全日)';
const DAY_RUN_SOURCE = `${DAY_TOKEN}(?:[・、,/~〜～-]?${DAY_TOKEN})*`;
const DAY_RUN = new RegExp(DAY_RUN_SOURCE, 'g');
// 曜日の字を含むが曜日ではない語。曜日を探す前に伏せる（「料金」の「金」を金曜にしない）
const NOT_DAYS = /料金|代金|現金|税金|返金|入金|年金|賃金|集金|金額|本日|毎日|前日|当日|翌日|今日|明日|昨日|終日|後日|先日|連日|期日|日本|日中|日時|日程|日帰り|日替|毎月|今月|来月|先月|正月|年月|月額|月間|土地|土産|水道|火災|木材|大晦日|晦日|月末|月初/g;
// ---- 日付 ----

const WEEKDAY = '(?:\\(([月火水木金土日])(?:曜日?)?(?:・?祝日?)?\\))?';
// 「8月1日(土)」「2026/9/29」「2026年12月29日」。曜日の括弧も日付の一部として扱う
const DATE = new RegExp(`(?:(\\d{4})[年/.])?(\\d{1,2})([月/])(\\d{1,2})日?${WEEKDAY}`, 'g');
// 月の付いた日付のすぐ後に「・」「、」「と」「～」で続く月のない日（「12月30日(水)・31日(木)」「1月1日(金)～3日(日)」）。
// 「12/30・31」の形は「日」がなくても読む
const NEXT_DAY = new RegExp(`^(\\s*(?:[・、,と&＆~〜～\\-－]|から|より)\\s*)(\\d{1,2})日${WEEKDAY}`);
const NEXT_SLASH_DAY = new RegExp(`^(\\s*(?:[・、,と&＆~〜～\\-－]|から|より)\\s*)(\\d{1,2})日?(?![\\d/月:時名人円分歳回枚])${WEEKDAY}`);
// 上の形で読めない、月だけ・日だけの日付（「12月中は」「31日は」）。期間を決められないので読めない告知とする
const LOOSE_DATE = /(?<![\d/.])\d{1,2}月(?![\d額間])|(?<![\d/.])\d{1,2}日(?!間|前|後|以内|以上|未満|目|中|分|毎|ごと|\d)/;

// 文の中の日付。{ year, month, day, weekday, start, end } の配列（月のない日は前の日付の年・月を引き継ぐ）
function dateTokens(text) {
  const tokens = [];
  for (const match of text.matchAll(DATE)) {
    let date = { year: match[1] ? Number(match[1]) : 0, month: Number(match[2]), day: Number(match[4]),
      weekday: match[5], start: match.index, end: match.index + match[0].length };
    tokens.push(date);
    const pattern = match[3] === '/' ? NEXT_SLASH_DAY : NEXT_DAY;
    for (let next = pattern.exec(text.slice(date.end)); next; next = pattern.exec(text.slice(date.end))) {
      const start = date.end + next[1].length;
      date = { year: date.year, month: date.month, day: Number(next[2]), weekday: next[3], start, end: date.end + next[0].length };
      tokens.push(date);
    }
  }
  return tokens.filter((date) => date.month >= 1 && date.month <= 12 && date.day >= 1 && date.day <= 31);
}

// 日付の部分を「#」に置き換える
function maskDates(text) {
  let out = '';
  let cursor = 0;
  for (const date of dateTokens(text)) {
    if (date.start < cursor) continue;
    out += `${text.slice(cursor, date.start)}#`;
    cursor = date.end;
  }
  return out + text.slice(cursor);
}

const mask = (text) => maskDates(text).replace(NOT_DAYS, (word) => '#'.repeat(word.length));

// 文の中の曜日の並び。{ text, days(Set | null) } の配列。曜日の集まりとして読めない語（「平日」など）は days が null
function dayRuns(masked) {
  const runs = [];
  for (const match of masked.matchAll(DAY_RUN)) {
    if (/\d/.test(masked[match.index - 1] ?? '')) continue; // 「1日」「8月」は日付
    runs.push({ text: match[0], days: parseDays(match[0]) });
  }
  return runs;
}

const sameSet = (a, b) => a.size === b.size && [...a].every((day) => b.has(day));
const signature = (columns) => columns.map((days) => [...days].sort().join('')).join('|');

// 備考の曜日の集まりが列の曜日で表せるか。列を半分だけ含む、列にない曜日を含む、なら食い違い
function fitsColumns(days, columns) {
  const union = new Set(columns.flatMap((column) => [...column]));
  if ([...days].some((day) => !union.has(day))) return false;
  return columns.every((column) => {
    const overlap = [...column].filter((day) => days.has(day)).length;
    return overlap === 0 || overlap === column.size;
  });
}

// ---- 最終受付 ----

const LAST_ENTRY = /最終受付(?:時間)?(?:は)?:?\(?(\d{1,2})(?::(\d{2})|時(?:(\d{1,2})分)?)\)?(?:まで)?/g;
const RECEPTION = /受付時間:?\(?(OPEN|\d{1,2}:\d{2})[-~〜～](CLOSE|\d{1,2}:\d{2})\)?/gi;
const clockText = (hour, minute) => `${Number(hour)}:${String(Number(minute ?? 0)).padStart(2, '0')}`;
const sameStart = (a, b) => (/^OPEN$/i.test(a) || /^OPEN$/i.test(b)
  ? a.toUpperCase() === b.toUpperCase()
  : clock(a) === clock(b));

// 時間帯の欄と備考から最終受付を読む。{ value, rest } か { reason }
function lastEntryOf(texts, start) {
  const times = new Set();
  let found = 0;
  let mismatch = false;
  const rest = texts.map((text) => text
    .replace(LAST_ENTRY, (_, hour, minute, minute2) => {
      found += 1;
      times.add(clockText(hour, minute ?? minute2));
      return '';
    })
    .replace(RECEPTION, (_, from, to) => {
      found += 1;
      if (!sameStart(from, start)) mismatch = true; // 受付の始まりが時間帯の始まりと違うと、入れる時刻を表せない
      if (!/^CLOSE$/i.test(to)) times.add(clockText(...to.split(':')));
      return '';
    }));
  const mentions = texts.join('').match(/受付/g)?.length ?? 0;
  if (mismatch || mentions !== found) return { reason: 'last-entry' };
  if (times.size > 1) return { reason: 'last-entry-varies' };
  return { value: [...times][0] ?? '', rest };
}

// ---- 1 人の追加料金 ----

const SOLO_WORD = /(?<!\d)(?:1名|1人|一人|ひとり)様?/;
// 「お一人様30分料金」「1名様あたり」は 1 人あたりの金額という意味で、1 人で使うときの条件ではない
const PER_PERSON = /お?(?:一人|ひとり)様(?:30分|\d+時間|フリータイム|パック|・|昼|夜|夕方|深夜)*料金|(?<!\d)(?:1名|1人|一人|ひとり)様?(?:あたり|当たり|につき)/g;
// 「上記は2名様以上で御利用頂く料金」「おひとり様…店舗まで直接お問合せ」。店全体で 1 人の金額を出さない
const GROUP_ONLY = /(?<!\d)[2-9]名様?以上|(?:1名|1人|一人|ひとり)様?[^。]{0,40}?(?:問い?合わ?せ|問合せ)/;
const SOLO = '(?:1名|1人|一人|ひとり)様?の?ご?';
const SOLO_RULES = [
  // 「土日祝の1名様利用は30分毎+100円の追加料金となります。」30 分ごとの加算
  { pattern: new RegExp(`(${DAY_RUN_SOURCE})の${SOLO}利用は30分(?:毎|ごと)に?\\+(\\d+)円`, 'g'), perSlot: true },
  // 「1名様のご利用の場合は下記の追加料金が発生します。（月～金・祝前）+200円（土・日・祝）+400円」
  { pattern: new RegExp(`${SOLO}利用の場合は[^。]*。?((?:[(【][^)】]+[)】]\\+\\d+円)+)`, 'g'), list: true },
  // 「【土・日・祝のみ】お部屋の数に限りがある為、1名様のご利用の場合+50円」
  { pattern: new RegExp(`【([^】]+?)のみ】[^。【]*?${SOLO}利用の場合\\+(\\d+)円`, 'g') },
];

// 備考の 1 人の追加料金。{ entries: [{ days, extra, perSlot }], unknown, rest }。文がなければ entries は空
function soloStatement(masked) {
  const entries = [];
  let rest = masked;
  for (const rule of SOLO_RULES) {
    rest = rest.replace(rule.pattern, (_, days, value) => {
      if (rule.list) {
        for (const item of days.matchAll(/[(【]([^)】]+)[)】]\+(\d+)円/g)) {
          entries.push({ days: parseDays(item[1]), extra: Number(item[2]), perSlot: false });
        }
      } else {
        entries.push({ days: parseDays(days), extra: Number(value), perSlot: Boolean(rule.perSlot) });
      }
      return '#';
    });
  }
  // 読めなかった 1 人の文が残っていれば、この表の 1 人の金額は決められない
  const unknown = SOLO_WORD.test(rest) || entries.some((entry) => !entry.days || !entry.extra);
  return { entries, unknown, rest };
}

// 列ごとに 1 人の加算額を決める。{ extra } か 'unknown'。30 分は「30分毎」、フリータイム・パックは 1 回の加算だけ読む
function soloColumns(columns, statement, slot) {
  return columns.map((days) => {
    if (statement.unknown) return 'unknown';
    const hits = statement.entries.filter((entry) => [...days].some((day) => entry.days.has(day)));
    if (!hits.length) return { extra: 0 };
    if (hits.length > 1 || !sameSet(hits[0].days, days) || hits[0].perSlot !== slot) return 'unknown';
    return { extra: hits[0].extra };
  });
}

// ---- 表 ----

function customerName(value) {
  const name = norm(value);
  if (name === '学生会員') return '学生会員';
  if (name === '学生' || name === '学生一般') return '学生';
  if (name === '会員') return '会員';
  if (name === '一般') return '一般';
  if (/^シニア(?:会員)?/.test(name)) return 'シニア';
  return null;
}

const DASHES = new Set(['-', '‐', '‑', '‒', '–', '—', '―', '−', 'ー']);

// 数字（とカンマと円）だけのセルを金額にする。「-」は提供なし。それ以外は読めない（null）
function cellValue(value) {
  const text = norm(value);
  if (DASHES.has(text)) return '-';
  const match = /^¥?(\d{1,3}(?:,\d{3})+|\d+)円?$/.exec(text);
  const price = match ? Number(match[1].replace(/,/g, '')) : null;
  return price !== null && price > 0 && price <= 30000 ? price : null;
}

// 表の列（曜日）と区分ごとの金額を読む。{ columns, mapped, values, offered } か { reason }
function readTable(section) {
  const columns = (section.header ?? []).slice(1).map((label) => parseDays(label));
  if (!columns.length || columns.some((days) => !days)) return { reason: 'days' };
  const mapped = dayLabels(columns);
  if (!mapped) return { reason: 'days' };
  const values = new Map();
  for (const row of section.rows ?? []) {
    const customer = customerName(row[0]);
    if (!customer || values.has(customer) || row.length !== columns.length + 1) return { reason: 'table' };
    values.set(customer, row.slice(1).map(cellValue));
  }
  // 一部店舗は「学生」の1列だけで、学生会員と学生を分けていない。その場合は同じ学生料金を両区分に割り当てる。
  if (values.has('学生') && !values.has('学生会員')) values.set('学生会員', values.get('学生'));
  if (values.has('学生会員') && !values.has('学生')) values.set('学生', values.get('学生会員'));
  if (CUSTOMERS.some((customer) => !values.has(customer))) return { reason: 'customers' };
  const cells = [...values.values()];
  if (cells.some((row) => row.some((value) => value === null))) return { reason: 'price-cell' };
  // 列のすべてが「-」なら、その曜日は提供なし。一部の区分だけ「-」の列は、ほかの区分の扱いが分からないので表ごと出さない
  const offered = columns.map((_, index) => {
    const dashes = cells.filter((row) => row[index] === '-').length;
    return dashes === 0 ? true : dashes === cells.length ? false : null;
  });
  if (offered.includes(null)) return { reason: 'partial-dash' };
  if (!offered.includes(true)) return { reason: 'no-price' };
  return { columns, mapped, values, offered };
}

const pricesAt = (table, index, extra = 0) => (table.offered[index]
  ? Object.fromEntries(CUSTOMERS.map((customer) => [customer, table.values.get(customer)[index] + extra]))
  : {});

function dayRows(table, pick) {
  const rows = table.columns.map((_, index) => ({ day: table.mapped.labels[index], prices: pick(index) }));
  if (table.mapped.partial) rows.push({ day: '平日', prices: {} });
  if (table.mapped.holidayUnknown) rows.unshift({ day: '祝', prices: {} });
  return rows;
}

const hasPrices = (rows) => rows.some((row) => Object.keys(row.prices).length);

// 時間帯の欄（.tbl-ttl）を、先頭のプラン名と時間帯に分ける。最終受付の部分は先に除く
function splitTime(text) {
  const withoutEntry = text.replace(LAST_ENTRY, '').replace(RECEPTION, '').replace(/\(\)/g, '');
  const lead = /^(.*?)(?=OPEN|\d{1,2}:\d{2}|\d{1,2}時(?!間)|$)/i.exec(withoutEntry)[1];
  return { name: lead.replace(/[(【[]+$/, ''), range: parseTimeRange(withoutEntry.slice(lead.length)) };
}

const SLOT_NAME = /^(?:朝|昼|夕方|夜|深夜)?(?:30分)?(?:料金)?$/;

// 表の種類と名前。プラン名は時間帯の欄の先頭語から取り、パックの時間は見出しと一致するものだけ使う
function planName(section, ttlName) {
  const title = norm(section.header?.[0]);
  if (/30分/.test(title)) return SLOT_NAME.test(ttlName) ? { slot: true, name: '30分' } : { reason: 'slot-name' };
  const pack = /(\d+)時間パック/.exec(title);
  const free = /フリータイム/.test(title);
  if (pack && free) return { reason: 'unsupported-name' };
  if (pack) {
    const name = ttlName || title;
    return name.includes(pack[0]) ? { slot: false, name } : { reason: 'name-mismatch' };
  }
  if (free) {
    const name = ttlName || title;
    return /パック|\d+時間/.test(name) ? { reason: 'name-mismatch' } : { slot: false, name };
  }
  return { reason: 'unsupported-name' };
}

// 1 つの表を読む。{ slot, plan, table, statement, runs, leftover, label } か { reason, label }。
// runs は時間帯の欄と備考にある曜日の並び、leftover はそのうち 1 人の追加料金の文で読めなかったもの
function readSection(section) {
  const time = norm(section.time);
  const { name: ttlName, range } = splitTime(time);
  const label = ttlName || norm(section.header?.[0]) || time;
  const kind = planName(section, ttlName);
  if (kind.reason) return { reason: kind.reason, label };
  if (!range) return { reason: 'time', label };
  const table = readTable(section);
  if (table.reason) return { reason: table.reason, label };
  const entry = lastEntryOf([time, norm(section.note)], range.start);
  if (entry.reason) return { reason: entry.reason, label };
  if (kind.slot && entry.value) return { reason: 'last-entry', label };
  const [timeRest, noteRest] = entry.rest;
  const note = mask(noteRest.replace(PER_PERSON, '#'));
  const statement = soloStatement(note);
  const timeRuns = dayRuns(mask(timeRest));
  return {
    slot: kind.slot,
    label,
    table,
    statement,
    runs: [...timeRuns, ...dayRuns(note)],
    leftover: [...timeRuns, ...dayRuns(statement.rest)],
    plan: {
      name: kind.name,
      hours: `${range.start === 'OPEN' ? '開店' : range.start}～${range.end === 'CLOSE' ? '閉店' : range.end}`,
      last_entry: entry.value,
      drink: '飲み放題込み',
    },
  };
}

// ---- 期間料金の告知 ----

const PERIOD_SOURCE = '年末年始|年末|年始|正月|大晦日|元日|元旦|三が日|お盆|夏季|冬季|夏期|冬期|春休み|夏休み|冬休み|GW|ゴールデンウィーク|シルバーウィーク|連休|繁忙期|特別料金|特別期間|期間限定|期間中|期間料金|期間の料金|料金改定|価格改定|改定|値上げ|値下げ|新料金|表示料金|料金変更|クリスマス|特定日|特別日|料金が異な|料金は異な|異なる料金';
const PERIOD_WORDS = new RegExp(PERIOD_SOURCE);
// 季節の語。過ぎた告知の「＜夏季期間の料金はこちら＞」を、同じ見出しの過ぎた告知と同じものとみなすのに使う
const SEASONS = /年末年始|年末|年始|正月|大晦日|元日|元旦|三が日|お盆|夏季|冬季|夏期|冬期|春休み|夏休み|冬休み|GW|ゴールデンウィーク|シルバーウィーク|連休|繁忙期|クリスマス/g;
// 日付のすぐ後に続く期間の語（「～9月6日(日)は夏季料金」の「は夏季」、「は年末年始特別料金」の「は年末年始特別料金」）
const ATTACHED = new RegExp(`^\\s*(?:の期間は|期間は|の間は|には|は|の)?\\s*(?:${PERIOD_SOURCE})(?:[のは・]?(?:${PERIOD_SOURCE}))*`);
// 日付の間の「～」。「12月29日～翌1月3日」も期間にする
const RANGE_JOIN = /^\s*(?:[~〜～\-－]|から|より)\s*(?:翌年?)?\s*$/;
const LIST_JOIN = /^\s*(?:[・、,と&＆~〜～\-－]|から|より)\s*(?:翌年?)?\s*$/;
// 告知を文に分ける。メガビッグは 1 つの見出しに <br> で複数の告知を並べ、「＜…の料金はこちら＞」のリンクを添える
const SENTENCE_END = /[。！!？?※＜＞<>\n]/;
const TAX_EXCLUDED = /税別|税抜|外税|\+税/;
const PRICE_FILE = /\.(?:pdf|png|jpe?g|gif|webp|svg)(?:[?#].*)?$/i;

function utcDay(year, month, day) {
  const time = Date.UTC(year, month - 1, day);
  const date = new Date(time);
  return date.getUTCMonth() === month - 1 && date.getUTCDate() === day ? time / DAY_MS : null;
}

function candidates(date, today) {
  if (date.year) return [utcDay(date.year, date.month, date.day)].filter((day) => day !== null);
  const year = new Date(today * DAY_MS).getUTCFullYear();
  return [year - 1, year, year + 1].map((y) => utcDay(y, date.month, date.day)).filter((day) => day !== null);
}

const nearest = (list, today) => [...list].sort((a, b) => Math.abs(a - today) - Math.abs(b - today))[0] ?? null;
const weekdayOf = (day) => WEEK[new Date(day * DAY_MS).getUTCDay()];

// 年か曜日が書いてある日付の日番号。年がなければ今日にいちばん近い年に置き、曜日がその年の暦に合わなければ null。
// 去年の曜日のまま今年も出した告知（「12月28日(日)～」を 2026 年に出す）を、去年の告知と読まないため。
// 年も曜日もなければ undefined（期間のもう一方の日付から決める）
function resolve(date, today) {
  if (date.year) {
    const day = utcDay(date.year, date.month, date.day);
    return day !== null && (!date.weekday || weekdayOf(day) === date.weekday) ? day : null;
  }
  if (!date.weekday) return undefined;
  const day = nearest(candidates(date, today), today);
  return day !== null && weekdayOf(day) === date.weekday ? day : null;
}

function rangeOf(first, second, today) {
  let from = resolve(first, today);
  let to = resolve(second, today);
  if (from === null || to === null) return null;
  if (from === undefined && to === undefined) from = nearest(candidates(first, today), today);
  if (from === undefined) from = candidates(first, today).filter((day) => day <= to).pop();
  if (to === undefined) to = candidates(second, today).find((day) => day >= from);
  if (from === undefined || from === null || to === undefined || to < from || to - from > 366) return null;
  return [from, to];
}

// 文の中の日付から期間を読む。[開始, 終了] の配列。読めない日付は null。
// 「X まで」だけの告知は、表示中の料金の期限か期間料金の終わりかを読み分けられないので、載っている間はずっと続くもの
// （[-∞, ∞]）とする。「X～」だけの告知は [X, ∞]
export function noticeWindows(text, today) {
  const t = soft(text);
  const dates = dateTokens(t);
  const windows = [];
  if (LOOSE_DATE.test(maskDates(t))) windows.push(null);
  for (let index = 0; index < dates.length; index += 1) {
    const date = dates[index];
    const next = dates[index + 1];
    const after = t.slice(date.end, next ? next.start : t.length);
    if (next && RANGE_JOIN.test(after)) {
      windows.push(rangeOf(date, next, today));
      index += 1;
      continue;
    }
    const known = resolve(date, today);
    const day = known === undefined ? nearest(candidates(date, today), today) : known;
    if (day === null) windows.push(null);
    else if (/^\s*(?:[~〜～]|から|より|以降)/.test(after)) windows.push([day, Infinity]);
    else if (/^\s*(?:まで|迄)/.test(after)) windows.push([-Infinity, Infinity]);
    else windows.push([day, day]);
  }
  return windows;
}

// 日付と、日付のすぐ後の期間の語を取り除いた残り
function residue(text) {
  const t = soft(text);
  const dates = dateTokens(t);
  let out = '';
  let cursor = 0;
  for (let index = 0; index < dates.length; index += 1) {
    const date = dates[index];
    if (date.start < cursor) continue;
    out += t.slice(cursor, date.start);
    cursor = date.end;
    const next = dates[index + 1];
    const after = t.slice(cursor, next ? next.start : t.length);
    if (next && LIST_JOIN.test(after)) continue; // 日付の並びの途中
    cursor += ATTACHED.exec(after)?.[0].length ?? 0;
  }
  return out + t.slice(cursor);
}

// 告知の文を見て、店ごと出さないかを決める。出さないなら { reason }
function periodNotice(blocks, today) {
  for (const block of blocks) {
    const text = soft(block);
    if (!text) continue;
    if (TAX_EXCLUDED.test(text)) return { reason: 'tax-excluded' };
    const past = new Set();
    const pointers = [];
    for (const sentence of text.split(SENTENCE_END).map((part) => part.trim()).filter(Boolean)) {
      const windows = noticeWindows(sentence, today);
      if (!windows.length) {
        if (!PERIOD_WORDS.test(sentence)) continue;
        // 「＜夏季期間の料金はこちら＞」は、同じ見出しの過ぎた告知のリンクなら無視する（下で確かめる）
        if (/こちら/.test(sentence)) { pointers.push(sentence); continue; }
        return { reason: 'period-notice' }; // 期間の分からない告知
      }
      if (windows.some((window) => window === null)) return { reason: 'period-notice' };
      // 今日以降と重なる告知は、先の告知でも出さない（アプリは先の日付を選べ、一覧に注意書きが出ないため）
      if (windows.some(([, to]) => to >= today)) return { reason: 'period-notice' };
      // 過ぎた告知。日付のすぐ後の期間の語のほかに期間の語が残れば、別の告知が同じ文にあるとみなす
      if (PERIOD_WORDS.test(residue(sentence))) return { reason: 'period-notice' };
      for (const word of sentence.match(SEASONS) ?? []) past.add(word);
    }
    for (const pointer of pointers) {
      const rest = pointer.replace(SEASONS, (word) => (past.has(word) ? '' : word)).replace(/期間(?:の)?料金|期間/g, '');
      if (PERIOD_WORDS.test(rest)) return { reason: 'period-notice' };
    }
  }
  return {};
}

// 表の外の文（見出し・お知らせ）に、曜日の語があれば読めない条件とみなす（日付の曜日の括弧は除く）
function noticeDays(blocks) {
  return blocks.some((block) => dayRuns(mask(norm(block))).length > 0);
}

// 表の外の文に 1 人の料金の文があるか（「お一人様30分料金」は 1 人あたりの金額という意味なので除く）
function soloNotice(blocks) {
  return blocks.some((block) => SOLO_WORD.test(mask(norm(block).replace(PER_PERSON, '#'))));
}

// ---- 店 ----

// 取得で捨てた表のうち、表はあるのに読めなかったもの（「1,400※」のような注記のあるセル、形の違う表）
const SKIPPED_UNREAD = new Set(['price-cell', 'shape']);
// 変換で読めなかった表のうち、注記のあるセルや見慣れない行（「年末年始」の行など）があるもの
const TABLE_UNREAD = new Set(['price-cell', 'table']);

// { regular, solo, week, alerts, dropped } か { reason, dropped } を返す。
// options: { today, notices, links, skipped, missing, unread_tables }（update_megabig.py が保存した記録をそのまま渡してよい）
export function convertMegabig(sections, hoursText, options = {}) {
  const skipped = options.skipped ?? [];
  const dropped = skipped.map((item) => ({ plan: norm(item.time) || norm(item.title), reason: item.reason }));
  if (options.missing) return { reason: `missing-${options.missing}`, dropped };
  if (options.unread_tables) return { reason: 'unread-table', dropped };
  // 読めなかったセルの注記や表の形は、ほかの表にも効く条件（期間料金など）かもしれないので店ごと出さない
  if (skipped.some((item) => SKIPPED_UNREAD.has(item.reason))) return { reason: 'unread-table', dropped };
  if ((options.links ?? []).some((href) => PRICE_FILE.test(String(href)))) return { reason: 'price-link', dropped };
  const today = jstDay(options.today);
  const list = sections ?? [];
  const notices = [...(options.notices ?? []), ...new Set(list.map((section) => section.heading ?? ''))];
  const skippedText = skipped.flatMap((item) => [item.time, item.title, item.note]);
  const sectionText = list.flatMap((section) => [section.time, section.note]);
  // 期間料金の告知は、表の外の文・捨てた表の備考・時間帯の欄・備考・表の見出しと行の名前から探す
  const blocks = [...notices, ...skippedText, ...sectionText,
    ...list.flatMap((section) => [...(section.header ?? []), ...(section.rows ?? []).map((row) => row?.[0])])];
  const notice = periodNotice(blocks, today);
  if (notice.reason) return { reason: notice.reason, dropped };
  if (noticeDays(notices)) return { reason: 'notice-days', dropped };
  const week = parseMegabigHours(hoursText);
  if (!week) return { reason: 'no-hours', dropped };

  const read = list.map(readSection);
  const unread = read.find((item) => TABLE_UNREAD.has(item.reason));
  if (unread) return { reason: 'unread-table', dropped: [...dropped, { plan: unread.label, reason: unread.reason }] };
  // 備考の曜日が列の曜日で表せない表は、同じ曜日の列を持つ表も含めて出さない（列の曜日の書き間違いを疑う）
  const conflicts = new Set();
  for (const item of read) {
    if (item.reason) continue;
    if (item.runs.some((run) => run.days && !fitsColumns(run.days, item.table.columns))) {
      conflicts.add(signature(item.table.columns));
    }
  }
  // 「2名様以上の料金」「おひとり様は店舗へお問合せ」と、表の外の 1 人の料金の文は、店全体で 1 人の金額を決められないものとする
  const groupOnly = soloNotice(notices)
    || [...notices, ...skippedText, ...sectionText].some((text) => GROUP_ONLY.test(norm(text)));
  const slotPlans = [];
  const fixedPlans = [];
  const soloSlot = [];
  const soloFixed = [];
  const seen = new Set();
  for (const item of read) {
    if (item.reason) {
      dropped.push({ plan: item.label, reason: item.reason });
      continue;
    }
    const drop = (reason) => dropped.push({ plan: item.label, reason });
    if (conflicts.has(signature(item.table.columns))) { drop('days-conflict'); continue; }
    if (item.leftover.length) { drop('note-days'); continue; }
    const key = `${item.slot}|${item.plan.name}|${item.plan.hours}`;
    if (seen.has(key)) { drop('duplicate'); continue; }
    seen.add(key);
    const { table, statement } = item;
    const solo = groupOnly
      ? table.columns.map(() => 'unknown')
      : soloColumns(table.columns, statement, item.slot);
    const soloDays = () => dayRows(table, (index) => (solo[index] === 'unknown' ? {} : pricesAt(table, index, solo[index].extra)));
    if (item.slot) {
      slotPlans.push({ ...item.plan, days: dayRows(table, (index) => pricesAt(table, index)) });
      // 1 人の文がある表だけ 1 人の行を作る。決められない曜日は空の行（アプリは注意書きを付けて 2 名以上の金額を出す）
      const mentioned = statement.entries.length > 0 || statement.unknown;
      if (mentioned && !groupOnly && hasPrices(soloDays())) soloSlot.push({ ...item.plan, days: soloDays() });
      continue;
    }
    // フリータイム・パックは 1 人の金額を決められない曜日の料金を出さない
    const days = dayRows(table, (index) => (solo[index] === 'unknown' ? {} : pricesAt(table, index)));
    if (!hasPrices(days)) { drop(groupOnly ? 'solo-unknown-shop' : 'solo-unknown'); continue; }
    if (solo.some((value) => value === 'unknown')) dropped.push({ plan: item.label, reason: 'solo-unknown-days', partial: true });
    fixedPlans.push({ ...item.plan, days });
    if (solo.some((value) => value !== 'unknown' && value.extra > 0)) soloFixed.push({ ...item.plan, days: soloDays() });
  }
  if (!list.some((section) => /30分/.test(norm(section.header?.[0])))) return { reason: 'no-slot-table', dropped };
  if (!slotPlans.length) return { reason: 'unreadable-slot-table', dropped };
  const tables = (slot, free) => [
    ...(slot.length ? [{ band: '30分', customers: CUSTOMERS, plans: slot, note: '' }] : []),
    ...(free.length ? [{ band: 'フリータイム', customers: CUSTOMERS, plans: free, note: '' }] : []),
  ];
  return {
    regular: tables(slotPlans, fixedPlans),
    solo: tables(soloSlot, soloFixed),
    week,
    alerts: [],
    dropped,
  };
}

// convert.mjs から呼ぶ。出さなかった店・表を警告に出し、変換結果をそのまま返す
export function reportMegabig(page, result) {
  const id = /\/shop\/(\d+)/.exec(page ?? '')?.[1];
  const name = id ? `mb-${id}` : page;
  if (result.reason) console.warn(`::warning::megabig/${name}: 料金を配りません (${result.reason})`);
  for (const item of result.dropped ?? []) {
    const what = item.partial ? `${item.plan} の一部の曜日` : item.plan;
    console.warn(`::warning::megabig/${name}: ${what} を配りません (${item.reason})`);
  }
  return result;
}
