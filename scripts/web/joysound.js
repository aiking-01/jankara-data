import { parseDays } from './bigecho.js';

// JOYSOUND 直営店の公式料金表（update_joysound_prices.py の記録）を engine.js の regular の形へ変換する。
// 誤った金額を出さないことを最優先にする。読めない店は「料金表を見る」になるだけで済む。
export const SCOPE = 'standard-rates-v2';
// アプリは配信データを最長 21 日使い、利用者は先の日付も選べるので、今日から 21 日先までの告知を見る
export const LOOKAHEAD_DAYS = 21;
const CUSTOMERS = ['学生会員', '学生', '会員', 'シニア', '一般'];
const DAY_ORDER = ['月', '火', '水', '木', '金', '土', '日', '祝', '祝前'];
const norm = (value) => (value ?? '').normalize('NFKC').replace(/\s+/g, '');

// 曜日区分の見出しを engine.js の曜日ラベルにする。読むのは「平日」「全日」と、曜日だけを並べた区分
// （「週末 (土・日・祝)」→「土日祝」）だけ。「平日 (東京ドームイベント日は週末料金)」「平日 (月〜木)」のように
// 例外や範囲の付いた「平日」は、engine.js が「ほかの行に当たらない日すべて」として使う「平日」と意味が違うので読まない
function dayLabel(value) {
  const text = norm(value);
  if (text === '平日' || text === '全日') return text;
  if (text.startsWith('平日') || !/^[日週末月火水木金土祝前・()〜~-]+$/.test(text)) return null;
  const days = parseDays(text);
  return days ? DAY_ORDER.filter((day) => days.has(day)).join('') : null;
}

const TOKENS = /祝前|[月火水木金土日祝]/g;
// 2 つの曜日ラベルが同じ日に当たり得るか。祝日・祝前日はどの曜日にも重なる
function overlap(a, b) {
  const x = new Set(a.match(TOKENS));
  const y = new Set(b.match(TOKENS));
  const special = (set) => set.has('祝') || set.has('祝前');
  return [...x].some((token) => y.has(token)) || ((special(x) || special(y)) && x.size > 0 && y.size > 0);
}

// 時間帯（昼・夜）ごとに、その時間帯の表を読むかを決める。次のどれかなら読まない（どの日にどの料金か決められない）
// - 読めない区分がある（「平日」はほかの区分に当たらない日すべてを表すので、読めない区分の日にまで平日料金が出る。
//   明示の区分も、読めない区分と同じ日に当たるかもしれない）
// - 「全日」とほかの区分が並ぶ、明示の区分どうしが同じ日に当たり得る（例:「月〜木」と「日・祝」の祝日の月曜）
// - どの区分にも祝日の記載がない
function readablePeriods(sections) {
  const periods = new Map();
  for (const section of sections ?? []) {
    if (!periods.has(section.period)) periods.set(section.period, { labels: new Set(), unreadable: false });
    const label = dayLabel(section.day);
    if (label) periods.get(section.period).labels.add(label);
    else periods.get(section.period).unreadable = true;
  }
  for (const info of periods.values()) {
    const labels = [...info.labels];
    const explicit = labels.filter((label) => label !== '平日' && label !== '全日');
    const allDay = labels.includes('全日');
    info.skip = info.unreadable || (allDay && labels.length > 1)
      || explicit.some((a, i) => explicit.slice(i + 1).some((b) => overlap(a, b)))
      || (!allDay && !labels.some((label) => label.includes('祝')));
  }
  return periods;
}

// 「※最終受付17:59」「(受付14:59迄)」「※受付は18時まで」→ 受付の最後の時刻。読めなければ ''
function lastEntryOf(text) {
  const explicit = /(?:最終受付|受付終了|受付時間)[:：]?([0-2]?\d:\d{2})/.exec(text)
    ?? /受付(?:は)?([0-2]?\d:\d{2})(?:迄|まで)/.exec(text);
  if (explicit) return explicit[1];
  const hour = /受付(?:は)?([0-2]?\d)時(?:迄|まで)/.exec(text);
  return hour ? `${hour[1]}:00` : '';
}

function planOf(label, period, openingHours = '') {
  const text = norm(label);
  // 学生だけのグループ、人数の条件、入室の条件など、条件付きのプランは読まない
  if (/JOY学|限定|\d+名/.test(text)) return [];
  // 「※金・土・祝前は未実施」「(日・祝は翌3:00迄)」「(金・土・祝前)」のように、表の曜日区分の一部の日だけ
  // 時間や有無が変わるプランは、区分のすべての日に同じ時間帯で出すと誤るので読まない
  if (/[月火水木金土日祝前]は|[月火水木金土日祝前]\)|のみ|未実施|除く/.test(text)) return [];
  // LASTは公式の営業時間を明示的に確認できた場合だけ料金時刻へ変換。
  // 24時間営業や曜日ごとに閉店が変わる場合は推測せずsource_ratesへ残す。
  const exact = /^(?:\d{1,2}:\d{2})[〜～~-](?:翌)?(\d{1,2}:\d{2})$/.exec(norm(openingHours));
  const close = exact?.[1] ?? null;
  const ranges = [...text.matchAll(/(\d{1,2}:\d{2})[〜～~-](?:翌)?(\d{1,2}:\d{2}|LAST)/gi)];
  const endAt = (range) => range[2].toUpperCase() === 'LAST' ? close : range[2];
  // 「受付9:00〜10:59」のような受付の時間帯は、プランの時間帯と見分けられないので読まない
  if (/受付(?:時間)?[:：]?[0-2]?\d:\d{2}[〜~-]/.test(text)) return [];
  const lastEntry = lastEntryOf(text);
  // 読めない受付の条件があるプランは、受付の後の時刻にも出てしまうので読まない
  if (text.includes('受付') && !lastEntry) return [];
  if (text.includes('30分')) {
    if (ranges.length !== 1 || !endAt(ranges[0])) return [];
    return [{ band: '30分', name: '30分', hours: `${ranges[0][1]}～${endAt(ranges[0])}`, last_entry: '' }];
  }
  if (!ranges.length || (!text.includes('フリータイム') && !text.includes('フリー'))) return [];
  return ranges.filter((range) => endAt(range) !== null).map((range) => {
    const startHour = Number(range[1].split(':')[0]);
    const name = text.includes('深夜') || startHour >= 21 ? '深夜フリータイム'
      : text.includes('ロング') ? 'ロングフリータイム'
        : text.includes('夕方') || (startHour >= 14 && startHour < 18) ? '夕方フリータイム'
          : text.includes('昼') || period === '昼' ? '昼フリータイム' : '夜フリータイム';
    return {
      band: 'フリータイム', name, hours: `${range[1]}～${endAt(range)}`, last_entry: lastEntry,
    };
  });
}

function priceMap(source) {
  const prices = {};
  if (Number.isInteger(source?.学生)) {
    prices['学生会員'] = source.学生;
    prices['学生'] = source.学生;
  }
  for (const key of ['シニア', '会員', '一般']) if (Number.isInteger(source?.[key])) prices[key] = source[key];
  return prices;
}

function drinkOf(source, rawPrices) {
  const values = Object.entries(source ?? {}).filter(([customer]) => Number.isInteger(rawPrices?.[customer]));
  const defaultValues = values.filter(([customer]) => customer === '会員' || customer === '一般').map(([, drink]) => drink);
  if (new Set(defaultValues).size > 1) return null;
  const drink = defaultValues[0] ?? '';
  const result = { drink };
  for (const [customer, property] of [['学生', 'student_drink'], ['シニア', 'senior_drink']]) {
    const hasPrice = Number.isInteger(rawPrices?.[customer]);
    const value = values.find(([name]) => name === customer)?.[1] ?? '';
    if (hasPrice && value !== drink) {
      if (!value) return null; // The schema cannot say a customer is exempt from the base drink rule.
      result[property] = value;
    }
  }
  return result;
}

// 1 ドリンク制の書き方（NFKC で空白を除いた後）。「ワンドリンクオーダー制」「1ドリンク制」「ワンオーダー制」「お一人様1ドリンク」など
const ONE_DRINK = /(?:ワン|1)(?:ドリンク)?オーダー|(?:ワン|1)ドリンク(?:制|必須|以上|注文|のご注文)|1人(?:様)?(?:ワン|1)(?:ドリンク|オーダー)|お一人様(?:ワン|1)(?:ドリンク|オーダー)/;
const ALL_BAR = /全プラン.*ドリンクバー(?:無料|付|込)/;
const BAR_EXCEPTION = /\(?ドリンクバー付き?(?:の)?プラン(?:は|を)除く\)?/g;
// 飲み物の決まりらしいのに、上のどちらとも読めない書き方。付け方を決められないので、その店は出さない
const RULE_HINT = /オーダー制|ドリンク制|ドリンク(?:の)?(?:ご)?注文|ドリンク代|ドリンク必須|オーダー必須|飲み物.*(?:必須|注文|制)/;
// 決まりが一部のプランや区分だけのものかもしれない語。これがあれば付け方を決められない
const PARTIAL = /除|以外|のみ|場合|限り|30分|フリー|パック|学生|シニア|会員|一般|昼|夜|深夜|平日|週末|[月火水木金土日祝]曜|祝日/;

// 料金欄の ※ 注記と備考から飲み物の決まりを読む。
// 'one': ドリンクバー付きでないプランは 1ドリンク別（「全プランワンドリンクオーダー制（ドリンクバー付きプランは除く）」）
// 'bar': 全プランドリンクバー付（「全プランドリンクバー無料」）、'': 記載なし、null: 付け方が決まらない
export function drinkRule(notes) {
  let rule = '';
  for (const raw of notes ?? []) {
    // 「(ドリンクバー付きプランは除く)」は 1 ドリンク制の例外の書き方なので、取り除いてから読む
    const text = norm(raw).replace(BAR_EXCEPTION, '');
    const one = ONE_DRINK.test(text);
    const bar = ALL_BAR.test(text);
    if (!one && !bar) {
      if (RULE_HINT.test(text)) return null;
      continue;
    }
    if (one && bar) return null;
    if (PARTIAL.test(text)) return null;
    const found = one ? 'one' : 'bar';
    if (rule && rule !== found) return null;
    rule = found;
  }
  return rule;
}

// 決まりを、表のセルに飲み物の印がない料金区分に当てはめる。プラン名に「ドリンクバー付」とあるプランはドリンクバー付
function withRule(drinks, prices, rule, label) {
  if (!rule) return drinks;
  const text = norm(label);
  const fill = rule === 'bar' || /ドリンクバー(?:付|込|無料)/.test(text) ? 'ドリンクバー付' : '1ドリンク別';
  const result = { ...(drinks ?? {}) };
  for (const customer of Object.keys(prices ?? {})) {
    if (Number.isInteger(prices[customer]) && !result[customer]) result[customer] = fill;
  }
  return result;
}

// 「平日」だけ・「土日祝」だけの行のプランは、配信中のアプリ（engine.js の pickDayRow）が 1 行だけのプランを
// 曜日を見ずに使うため、別の曜日にも金額が出てしまう。同じ時間帯（昼・夜）の表にあるほかの曜日区分と「平日」を
// 空の行で足し、その日には金額がないことを表す（古いアプリでも別の曜日に金額が出ない）。
// 「平日」の行は、ほかの行に当たらない日すべてを表す（行が 2 つ以上のとき。engine.js の pickDayRow のとおり）
function fillDays(days, labels) {
  const present = new Set(days.map((item) => item.day));
  if (present.has('全日')) return days;
  const wanted = [...labels, '平日'].filter((label) => label !== '全日' && !present.has(label));
  return [...days, ...[...new Set(wanted)].map((day) => ({ day, prices: {} }))];
}

export function convertJoysound(sections, openingHours = '', { notes } = {}) {
  const rules = new Map();
  for (const [period, lines] of Object.entries(notes ?? {})) {
    const rule = drinkRule(lines);
    if (rule === null) return { reason: 'drink-rule-unclear' };
    rules.set(period, rule);
  }
  const periods = readablePeriods(sections);

  const groups = new Map();
  const conflicts = new Set();
  for (const section of sections ?? []) {
    const day = dayLabel(section.day);
    const info = periods.get(section.period);
    if (!day || !Array.isArray(section.rows) || info.skip) continue;
    const rule = rules.get(section.period) ?? '';
    for (const row of section.rows) {
      const prices = priceMap(row.prices);
      if (!Object.keys(prices).length) continue;
      const conditions = drinkOf(withRule(row.drinks, row.prices, rule, row.label), row.prices);
      if (!conditions) continue;
      for (const plan of planOf(row.label, section.period, openingHours)) {
        const key = `${plan.band}|${plan.name}|${plan.hours}|${plan.last_entry}|${JSON.stringify(conditions)}`;
        if (conflicts.has(key)) continue;
        if (!groups.has(key)) groups.set(key, { plan: { ...plan, ...conditions }, days: [], period: section.period });
        const group = groups.get(key);
        const existing = group.days.find((item) => item.day === day);
        // 昼と夜の表に同じプランがあると、足す空の行の区分が決まらない。同じ区分で金額が違う行も決められない
        if (group.period !== section.period || (existing && JSON.stringify(existing.prices) !== JSON.stringify(prices))) {
          groups.delete(key);
          conflicts.add(key);
        } else if (!existing) {
          group.days.push({ day, prices });
        }
      }
    }
  }

  const regular = [];
  for (const band of ['30分', 'フリータイム']) {
    const plans = [...groups.values()].filter((group) => group.plan.band === band && group.days.length)
      .map(({ plan: { band: _band, ...plan }, days, period }) => ({ ...plan, days: fillDays(days, periods.get(period).labels) }));
    if (plans.length) regular.push({ band, customers: CUSTOMERS, plans, note: '' });
  }
  return regular.length ? { regular, solo: [], source_rates: sections ?? [] } : { reason: 'no-readable-rates' };
}

// ---- 期間告知と店の状態 ----

// JST の日付 (YYYY-MM-DD)
export function jstDate(ms) {
  return new Date(ms + 9 * 3600 * 1000).toISOString().slice(0, 10);
}

// 告知の期間が today（JST）から 21 日先までと重なるか。始まり・終わりが読めない告知は重なるとみなす
export function seasonOverlaps(notices, today) {
  const [year, month, day] = today.split('-').map(Number);
  const start = Date.UTC(year, month - 1, day) / 1000 - 9 * 3600;
  const end = start + (LOOKAHEAD_DAYS + 1) * 86400;
  return notices.some((notice) => {
    const from = Number.isInteger(notice?.from) ? notice.from : null;
    const until = Number.isInteger(notice?.until) ? notice.until : null;
    return (from === null || from < end) && (until === null || until > start);
  });
}

// 店の記録 1 件を変換する。料金表を読めて（status が priced）、期間告知が今日から 21 日先までと重ならない店だけ。
// 状態のない旧形式の記録（期間告知や問い合わせ、飲み物の注記を確かめていない表）は金額にしない
export function convertJoysoundShop(shop, { today } = {}) {
  if (!shop || shop.scope !== SCOPE) return { reason: 'old-record' };
  if (shop.status !== 'priced') return { reason: typeof shop.status === 'string' ? shop.status : 'no-status' };
  if (!Array.isArray(shop.notices) || !/^\d{4}-\d{2}-\d{2}$/.test(today ?? '')) return { reason: 'season-unchecked' };
  if (seasonOverlaps(shop.notices, today)) return { reason: 'season' };
  if (!shop.notes || typeof shop.notes !== 'object' || Array.isArray(shop.notes)
    || Object.values(shop.notes).some((lines) => !Array.isArray(lines))) return { reason: 'drink-rule-unchecked' };
  return convertJoysound(shop.sections, shop.opening_hours, { notes: shop.notes });
}
