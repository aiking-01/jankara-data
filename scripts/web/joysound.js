import { parseDays } from './bigecho.js';

const CUSTOMERS = ['学生会員', '学生', '会員', 'シニア', '一般'];
const DAY_ORDER = ['月', '火', '水', '木', '金', '土', '日', '祝', '祝前'];
const norm = (value) => (value ?? '').normalize('NFKC').replace(/\s+/g, '');

function dayLabel(value) {
  const text = norm(value);
  if (/^平日/.test(text)) return '平日';
  if (/^全日/.test(text)) return '全日';
  const days = parseDays(text);
  return days ? DAY_ORDER.filter((day) => days.has(day)).join('') : null;
}

function planOf(label, period) {
  const text = norm(label);
  if (/JOY学|学生限定|\d+名様/.test(text)) return [];
  const ranges = [...text.matchAll(/(\d{1,2}:\d{2})[〜～~-](?:翌)?(\d{1,2}:\d{2})/g)];
  const lastEntry = /(?:最終受付|受付終了|受付時間)[:：]?([0-2]?\d:\d{2})/.exec(text)?.[1] ?? '';
  if (text.includes('30分')) {
    if (ranges.length !== 1) return [];
    return [{ band: '30分', name: '30分', hours: `${ranges[0][1]}～${ranges[0][2]}`, last_entry: '' }];
  }
  if (!ranges.length || (!text.includes('フリータイム') && !text.includes('フリー'))) return [];
  return ranges.map((range) => {
    const startHour = Number(range[1].split(':')[0]);
    const name = text.includes('深夜') || startHour >= 21 ? '深夜フリータイム'
      : text.includes('ロング') ? 'ロングフリータイム'
        : text.includes('夕方') || (startHour >= 14 && startHour < 18) ? '夕方フリータイム'
          : text.includes('昼') || period === '昼' ? '昼フリータイム' : '夜フリータイム';
    return {
      band: 'フリータイム', name, hours: `${range[1]}～${range[2]}`, last_entry: lastEntry,
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

export function convertJoysound(sections) {
  const groups = new Map();
  const conflicts = new Set();
  for (const section of sections ?? []) {
    const day = dayLabel(section.day);
    if (!day || !Array.isArray(section.rows)) continue;
    for (const row of section.rows) {
      const prices = priceMap(row.prices);
      if (!Object.keys(prices).length) continue;
      const conditions = drinkOf(row.drinks, row.prices);
      if (!conditions) continue;
      for (const plan of planOf(row.label, section.period)) {
        const key = `${plan.band}|${plan.name}|${plan.hours}|${plan.last_entry}|${JSON.stringify(conditions)}`;
        if (conflicts.has(key)) continue;
        if (!groups.has(key)) groups.set(key, { ...plan, ...conditions, days: [] });
        const group = groups.get(key);
        const existing = group.days.find((item) => item.day === day);
        if (existing) {
          if (JSON.stringify(existing.prices) !== JSON.stringify(prices)) {
            groups.delete(key); // Conflicting duplicate rows are ambiguous; omit this plan.
            conflicts.add(key);
          }
        } else {
          group.days.push({ day, prices });
        }
      }
    }
  }

  const regular = [];
  for (const band of ['30分', 'フリータイム']) {
    const plans = [...groups.values()].filter((item) => item.band === band && item.days.length)
      .map(({ band: _band, ...plan }) => plan);
    if (plans.length) regular.push({ band, customers: CUSTOMERS, plans, note: '' });
  }
  return regular.length ? { regular, solo: [] } : { reason: 'no-readable-rates' };
}
