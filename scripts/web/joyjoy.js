// JOYJOY は公式に「～」付きの最低価格しかないので、計算用の表（regular）には変換しない。
// 取得した記録（source/joyjoy_prices.json）から、変換する日に配ってよい店だけを選んでそのまま配る。
// - 料金なしの記録（ページは取れたが料金表がない・読めない・開店前など）は配らない
// - 年末年始・特別料金・料金改定などの告知があり、その期間がまだ終わっていない店は、始まりが先でも配らない
//   （利用者はアプリで先の日付をいくらでも選べるため）。終わりの日付が読めない告知がある店も配らない
// - 最低価格がありえない金額の店は配らない（validate_data.py の JOYJOY の検査と同じ範囲）
// 年のない日付は今日に近い回として読むので、1 年先までを見れば足りる
export const HORIZON_DAYS = 366;
const SLOT_YEN = [80, 1000];
const FREE_YEN = [300, 5000];
const ISO = /^\d{4}-\d{2}-\d{2}$/;
function validDay(day) {
  if (typeof day !== 'string' || !ISO.test(day)) return false;
  const time = Date.parse(`${day}T00:00:00Z`);
  return !Number.isNaN(time) && new Date(time).toISOString().slice(0, 10) === day;
}

// 変換する日（JST の YYYY-MM-DD）。テストや手で動かすときは環境変数 CONVERT_TODAY で差し替える
export function convertDay(env = globalThis.process?.env ?? {}, now = Date.now()) {
  const given = env.CONVERT_TODAY;
  if (given && validDay(given)) return given;
  return new Date(now + 9 * 3600 * 1000).toISOString().slice(0, 10);
}

function addDays(day, days) {
  return new Date(Date.parse(`${day}T00:00:00Z`) + days * 86400000).toISOString().slice(0, 10);
}

// 変換する日より後まで効く告知を返す。終わりの日付が読めない告知は、いつでも効くとみなす
export function activeNotice(notices, today) {
  const last = addDays(today, HORIZON_DAYS);
  for (const notice of notices ?? []) {
    const from = notice?.from ?? null;
    const until = notice?.until ?? null;
    if (!validDay(until) || (from !== null && !validDay(from))) return notice ?? {};
    if ((from ?? '0000-01-01') <= last && until >= today) return notice;
  }
  return null;
}

function rejection(record, today) {
  if (!record || typeof record !== 'object') return ['記録が不正', ''];
  if (!Array.isArray(record.rates) || !record.rates.length) return ['料金なし', record.withdrawn ?? ''];
  if (record.notices !== undefined && !Array.isArray(record.notices)) return ['告知の形式が不正', ''];
  const notice = activeNotice(record.notices, today);
  if (notice) return ['期間の告知がまだ終わっていない', String(notice.text ?? '')];
  for (const rate of record.rates) {
    const [low, high] = rate?.period === 'freetime' ? FREE_YEN : SLOT_YEN;
    if (!Array.isArray(rate?.windows) || !rate.windows.length) return ['時間帯がない', rate?.period ?? ''];
    for (const window of rate.windows) {
      const value = window?.minimum_yen;
      if (!Number.isInteger(value) || value < low || value > high) return ['最低価格が不自然', `${rate.period} ${value}`];
    }
  }
  return null;
}

// 配る店と、配らない店（id, kind, detail）を返す。配る店の形は元データと同じ（告知と取得の情報は外す）
export function convertJoyjoyShops(records, today) {
  const shops = {};
  const rejected = [];
  for (const [id, record] of Object.entries(records ?? {})) {
    const reason = rejection(record, today);
    if (reason) {
      rejected.push({ id, kind: reason[0], detail: reason[1].slice(0, 80) });
      continue;
    }
    shops[id] = { page: record.page, rates: record.rates, note: record.note, checked_at: record.checked_at };
  }
  return { shops, rejected };
}
