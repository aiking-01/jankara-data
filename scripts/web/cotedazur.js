// コート・ダジュール公式料金JSONを engine.js の regular / solo 形式へ変換する。
// 通常料金とヒトカラ料金は別の公式欄から読み、30分料金だけを計算用にする。
//
// 読むのは「通常料金」1 期間だけの店。年末年始などの期間別の料金は、日付で効く料金表がアプリにないので読まない
// （その店は「料金表を見る」になる）。アプリの日付選びには上限がなく、利用者は何か月先の日付も選べるので、
// 通常料金 1 期間でも、次のどれかに当たれば読まない。
// - start_date_time が変換する日より後、または end_date_time が書かれている（終わりの後の料金が分からない）
// - 注記などに年末年始・特別料金・料金改定などの告知があり、その期間がまだ終わっていない（前日の夜の残りも含む）、
//   または期間の終わりを読めない
// - 「税込」と書かれていない、または「税抜」「税別」と書かれている
// - 昼夜の切り替え（switching_time など）が想定の形でない
// 変換する日は JST の今日。テストや手で動かすときは引数か環境変数 CONVERT_TODAY（YYYY-MM-DD）で差し替える。
// 夜の 1 ドリンク制は、まねきねこと同じく drink を「1ドリンク別」にする（金額は足さない）。

const CUSTOMERS = ['学生会員', '会員', '一般'];
const PRICE_FIELDS = [
  ['学生会員', 'student_30'],
  ['会員', 'normal_30'],
  ['一般', 'ordinary_30'],
];
const PERIODS = [
  {
    night: false, drink: 'noon_drink',
    days: [
      { key: 'noon_weekend', day: '土日祝' },
      { key: 'noon_weekday', day: '月火水木金祝前' },
    ],
  },
  {
    night: true, drink: 'night_drink',
    // 祝前日は木曜など平日にもなるので、週末側を先に判定する。
    days: [
      { key: 'night_weekend', day: '金土日祝祝前' },
      { key: 'night_weekday', day: '月火水木' },
    ],
  },
];
// 夜の時間帯は翌朝まで続くので、前日に終わった期間は今日の朝まで効いているとみなす
const OVERNIGHT_DAYS = 1;
// 期間の告知とみなす言葉。学生の「グループ限定」などに当たらないよう、期間を表す言葉に絞る
const NOTICE = /年末|年始|年明け|大晦日|クリスマス|特別料金|特別期間|特別営業|特定日|改定|改訂|値上げ|値下げ|料金変更|繁忙|お盆|ゴールデンウィーク|GW|期間中|期間限定|別料金|料金が異な|料金が変|臨時|イベント/;
// 言葉がなくても、日付の書かれた行は期間の告知として扱う（今の公式の注記には日付の行がない）。
// 「12月24日」「12/24」「12.24」の形を日付とみなす（「1,610」のような金額や「18:00」の時刻には当たらない）
const DATED = /\d{1,2}月\d{1,2}日|(?:^|[^\d.,/])\d{1,2}[/.]\d{1,2}(?![\d.,/])/;
const TAX_EXCLUDED = /税抜|税別|外税|\+税/;
const DAY_MS = 86400000;

const normalized = (value) => String(value ?? '').normalize('NFKC').trim();

function amount(value, path) {
  if (value === null || value === undefined) return null;
  const text = normalized(value).replace(/,/g, '');
  if (!text || ['-', 'ー', '－'].includes(text)) return null;
  if (!/^\d+$/.test(text)) throw new Error(`${path}: 金額の形式が不正 (${value})`);
  return Number(text);
}

function oneRow(plan, key) {
  const rows = plan[key];
  if (!Array.isArray(rows) || rows.length !== 1 || !rows[0] || typeof rows[0] !== 'object') {
    throw new Error(`${key}: 料金表の行数が想定外です`);
  }
  return rows[0];
}

function prices(row, path) {
  const result = {};
  for (const [customer, field] of PRICE_FIELDS) {
    const value = amount(row[field], `${path}.${field}`);
    if (value !== null) result[customer] = value;
  }
  if (Object.keys(result).length !== PRICE_FIELDS.length) throw new Error(`${path}: 30分料金の区分が不足しています`);
  return result;
}

// まねきねこ（web/maneki.js）と同じく、1 ドリンク制なら drink を「1ドリンク別」にする。
// ドリンクの金額は足さない（公式の金額はドリンクバーの料金で、1 ドリンクの最低額ではないため）
function drinkText(plan, key) {
  const rows = plan[key];
  if (!Array.isArray(rows) || rows.length !== 1 || !rows[0]) return '飲み物条件は公式料金表で確認';
  const kind = normalized(rows[0].drink_type);
  if (/1ドリンク|1オーダー|ワンオーダー|ワンドリンク/.test(kind)) return '1ドリンク別';
  return kind || '飲み物条件は公式料金表で確認';
}

// 昼夜の切り替え時刻。公式の表示（pricelist.js）と同じく、昼は開店から、夜は閉店まで
function switchingClock(plan) {
  if (plan.switching_noon !== 'OPEN' || plan.switching_night !== 'CLOSE') {
    throw new Error('昼夜の時間帯が開店・閉店で区切られていません');
  }
  const match = typeof plan.switching_time === 'string' && /^(\d{2}):(00|30)(?::00)?$/.exec(plan.switching_time);
  if (!match || Number(match[1]) < 15 || Number(match[1]) > 21) {
    throw new Error(`昼夜の切り替え時刻を読めません (${plan.switching_time})`);
  }
  return `${match[1]}:${match[2]}`;
}

function slotPlan(plan, period, clock, solo = false) {
  const prefix = solo ? 'solo_' : '';
  const days = period.days.map(({ key, day }) => ({
    day,
    prices: prices(oneRow(plan, `${prefix}${key}`), `${key}.${solo ? 'solo' : 'regular'}`),
  }));
  return {
    name: '30分',
    hours: period.night ? `${clock}～閉店` : `開店～${clock}`,
    last_entry: '',
    days,
    drink: drinkText(plan, period.drink),
  };
}

// ---- 日付 ----

// 1970-01-01 からの日数（暦の上の日付だけを扱う）
function dayNumber(year, month, day) {
  const date = new Date(Date.UTC(year, month - 1, day));
  if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) return null;
  return date.getTime() / DAY_MS;
}

function isoDay(text) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(text ?? ''));
  return match ? dayNumber(Number(match[1]), Number(match[2]), Number(match[3])) : null;
}

// 変換する日（JST の今日）。テストや手で動かすときは引数か環境変数 CONVERT_TODAY（YYYY-MM-DD）で差し替える
export function todayOf(today) {
  const given = today ?? (typeof process !== 'undefined' ? process.env.CONVERT_TODAY : undefined);
  if (given) {
    const day = isoDay(given);
    if (day === null) throw new Error(`変換する日の形式が不正です (${given})`);
    return day;
  }
  return Math.floor((Date.now() + 9 * 3600000) / DAY_MS);
}

// 公式の '2026-10-01 10:00' を日番号にする。読めなければ null（公式の pricelist.js の parseDateTime と同じ書き方を受け付ける）
function officialDay(text) {
  const match = /^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::\d{2})?)?$/.exec(normalized(text));
  if (!match) return null;
  const day = dayNumber(Number(match[1]), Number(match[2]), Number(match[3]));
  if (day === null || Number(match[4] ?? 0) > 23 || Number(match[5] ?? 0) > 59) return null;
  return day;
}

// 期間が、今日から先のすべての日を覆っているか。覆っていなければ理由を返す。
// 公式の pricelist.js も start_date_time / end_date_time で表示する期間を決めている（空なら制限なし）。
// アプリの日付選びには上限がないので、終わりの日時が書かれていれば、それが何か月先でも読まない
function outsidePlan(plan, today) {
  const blank = (value) => value === '' || value === null || value === undefined;
  if (!blank(plan.start_date_time)) {
    const start = officialDay(plan.start_date_time);
    if (start === null) return `料金期間の始まりを読めません (${plan.start_date_time})`;
    if (start >= today) return `料金期間がまだ始まっていません (${plan.start_date_time})`;
  }
  if (!blank(plan.end_date_time)) return `料金期間の終わりが決まっています (${plan.end_date_time})`;
  return null;
}

// 年のない月日は、今日にいちばん近い年にする
function nearestDay(year, month, day, today) {
  if (year) return dayNumber(year, month, day);
  const base = new Date(today * DAY_MS).getUTCFullYear();
  let best = null;
  for (const candidate of [base - 1, base, base + 1]) {
    const value = dayNumber(candidate, month, day);
    if (value !== null && (best === null || Math.abs(value - today) < Math.abs(best - today))) best = value;
  }
  return best;
}

const DATE = /(?:(\d{4})年)?(?:(\d{1,2})月)?(\d{1,2})日|(?<![\d.,/])(?:(\d{4})[/.])?(\d{1,2})[/.](\d{1,2})(?![\d.,/])/g;
// 日付の後ろの「(土)」「(祝)」などの曜日は読み飛ばす（NFKC で全角の括弧も半角になる）
const WEEKDAY = String.raw`(?:\([月火水木金土日祝・]{1,4}\))?`;
const RANGE_GAP = new RegExp(String.raw`^\s*${WEEKDAY}\s*(?:[~〜\-－]|から)\s*$`);
const LIST_GAP = new RegExp(String.raw`^\s*${WEEKDAY}\s*[、,・]\s*$`);
const OPEN_END = new RegExp(String.raw`^\s*${WEEKDAY}\s*(?:以降|以後|から|より|[~〜\-－])`);
// 「1月11日まで」「(~1月11日)」のように終わりの側だけが書かれた日付
const UNTIL = new RegExp(String.raw`^\s*${WEEKDAY}\s*(?:まで|迄)`);
const FROM_BEFORE = /[~〜\-－]\s*$/;

// 告知の文から期間を [始まり, 終わり] で取り出す（分からない側は null）。
// 「12月11日、14日～17日」「12月24日、25日」のように「、」で並べた日はその 1 日、「1月11日まで」「～1月11日」は
// その日に終わる期間、それ以外で終わりが書かれていない日付（「12月1日より」「12月24日は」など）は、
// その日から終わりの分からない期間とする
function noticeRanges(text, today) {
  const dates = [];
  let month = null;
  for (const match of text.matchAll(DATE)) {
    const slash = match[5] !== undefined;
    const year = Number(slash ? match[4] ?? 0 : match[1] ?? 0) || null;
    const m = Number(slash ? match[5] : match[2] ?? month);
    const d = Number(slash ? match[6] : match[3]);
    if (!m) return null; // 月が分からない
    month = m;
    dates.push({ year, month: m, day: d, at: match.index, end: match.index + match[0].length });
  }
  const ranges = [];
  for (let i = 0; i < dates.length; i += 1) {
    const first = dates[i];
    const start = nearestDay(first.year, first.month, first.day, today);
    if (start === null) return null;
    const next = dates[i + 1];
    const before = text.slice(i > 0 ? dates[i - 1].end : 0, first.at);
    const listed = i > 0 && LIST_GAP.test(before);
    const after = text.slice(first.end, next ? next.at : text.length);
    if (UNTIL.test(after) || (!listed && FROM_BEFORE.test(before))) {
      ranges.push([null, start]);
    } else if (next && RANGE_GAP.test(after)) {
      let last;
      if (next.year) last = dayNumber(next.year, next.month, next.day);
      else {
        const startYear = new Date(start * DAY_MS).getUTCFullYear();
        last = dayNumber(startYear, next.month, next.day);
        if (last !== null && last < start) last = dayNumber(startYear + 1, next.month, next.day);
      }
      if (last === null || last < start) return null;
      ranges.push([start, last]);
      i += 1;
    } else if ((next && LIST_GAP.test(after)) || (listed && !OPEN_END.test(after))) {
      ranges.push([start, start]);
    } else {
      ranges.push([start, null]);
    }
  }
  return ranges;
}

function strings(value, found = []) {
  if (typeof value === 'string') found.push(value);
  else if (Array.isArray(value)) value.forEach((item) => strings(item, found));
  else if (value && typeof value === 'object') Object.values(value).forEach((item) => strings(item, found));
  return found;
}

// 期間の告知のうち、まだ終わっていない（前日の夜の残りも含む）もの、または読めないものがあれば理由を返す。
// アプリの日付選びには上限がないので、何か月先の期間でも、告知がある間は読まない
function noticeProblem(plan, today) {
  const ended = today - OVERNIGHT_DAYS;
  // 期間の日時の項目は outsidePlan が見る
  const { start_date_time: _from, end_date_time: _until, ...texts } = plan;
  const lines = strings(texts).flatMap((raw) => normalized(raw).split('\n')).map((line) => line.trim());
  for (const text of lines) {
    if (!NOTICE.test(text) && !DATED.test(text)) continue;
    const ranges = noticeRanges(text, today);
    if (!ranges || !ranges.length) return `期間の告知があり、期間を読めません (${text.slice(0, 40)})`;
    for (const [, last] of ranges) {
      if (last === null) return `期間の告知があり、終わりの日を読めません (${text.slice(0, 40)})`;
      if (last >= ended) return `終わっていない期間の告知があります (${text.slice(0, 40)})`;
    }
  }
  return null;
}

function taxProblem(plan) {
  if (strings(plan).some((text) => TAX_EXCLUDED.test(normalized(text)))) return '税抜の料金が含まれます';
  // 注記の「価格はすべて税込です」は、ひとりカラオケの料金にもかかる
  if (!normalized(plan.all_annotation_note).includes('税込')) return '税込と書かれていません';
  return null;
}

export function convertCotedazur(shop, { today } = {}) {
  const code = shop?.shop_code;
  if (shop?.no_price) throw new Error(`${code}: 公式の料金表なし`);
  const plans = shop?.price_plan;
  // 期間別の料金（年末年始など）は、日付で効く料金表がアプリにないので読まない
  if (!Array.isArray(plans) || plans.length !== 1 || normalized(plans[0]?.label_name) !== '通常料金') {
    throw new Error(`${code}: 料金期間が複数、または通常料金以外です`);
  }
  const plan = plans[0];
  if (plan.day_of_week_select !== true) throw new Error(`${code}: 曜日別料金の構成が変わりました`);
  const day = todayOf(today);
  const problem = outsidePlan(plan, day) ?? noticeProblem(plan, day) ?? taxProblem(plan);
  if (problem) throw new Error(`${code}: ${problem}`);
  let clock;
  try {
    clock = switchingClock(plan);
  } catch (error) {
    throw new Error(`${code}: ${error.message}`);
  }
  const regular = [{
    band: '通常料金',
    customers: CUSTOMERS,
    plans: PERIODS.map((period) => slotPlan(plan, period, clock)),
    note: plan.all_annotation_note ?? '',
  }];
  const solo = [{
    band: '通常料金',
    customers: CUSTOMERS,
    plans: PERIODS.map((period) => slotPlan(plan, period, clock, true)),
    note: plan.all_annotation_note ?? '',
  }];
  return { regular, solo };
}

/**
 * 一部の店舗で公式JSONの形式が変わっても、正常な店舗の料金は維持する。
 * 大量脱落は配信事故として拒否し、旧データを残す。
 * 配信ファイルの形（公式JSONの店舗コード・店舗名・price_plan も入れる）は今までどおりにする。アプリが使うのは
 * regular / solo だけだが、項目を外すと validate_data.py の「変更店舗が35%を超える」が全店に当たり、
 * --repair なしの検査（data-safety.yml）が通らなくなるため。
 */
export function convertCotedazurShops(records, minimumRatio = 0.9, { today } = {}) {
  if (!records || typeof records !== 'object' || Array.isArray(records)) {
    throw new Error('cotedazur: invalid source shops');
  }
  const day = todayOf(today);
  const iso = new Date(day * DAY_MS).toISOString().slice(0, 10);
  const shops = {};
  const rejected = [];
  for (const [id, shop] of Object.entries(records)) {
    try {
      const converted = convertCotedazur(shop, { today: iso });
      const { page, etag, last_modified, checked_at, ...officialData } = shop;
      shops[id] = { ...officialData, ...converted, checked_at };
    } catch (error) {
      rejected.push({ id, reason: String(error?.message ?? error) });
    }
  }
  const original = Object.keys(records).length;
  const accepted = Object.keys(shops).length;
  if (!original || accepted < Math.ceil(original * minimumRatio)) {
    throw new Error(`cotedazur: converted ${accepted}/${original} below safety threshold`);
  }
  return { shops, rejected, accepted, original };
}
