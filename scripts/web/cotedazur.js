// コート・ダジュール公式料金JSONを engine.js の regular / solo 形式へ変換する。
// 通常料金とヒトカラ料金は別の公式欄から読み、30分料金だけを計算用にする。

const CUSTOMERS = ['学生会員', '会員', '一般'];
const PRICE_FIELDS = [
  ['学生会員', 'student_30'],
  ['会員', 'normal_30'],
  ['一般', 'ordinary_30'],
];
const PERIODS = [
  {
    hours: '開店～18:00', drink: 'noon_drink',
    days: [
      { key: 'noon_weekend', day: '土日祝' },
      { key: 'noon_weekday', day: '月火水木金祝前' },
    ],
  },
  {
    hours: '18:00～閉店', drink: 'night_drink',
    // 祝前日は木曜など平日にもなるので、週末側を先に判定する。
    days: [
      { key: 'night_weekend', day: '金土日祝祝前' },
      { key: 'night_weekday', day: '月火水木' },
    ],
  },
];

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

function drinkText(plan, key) {
  const rows = plan[key];
  if (!Array.isArray(rows) || rows.length !== 1 || !rows[0]) return '飲み物条件は公式料金表で確認';
  const kind = normalized(rows[0].drink_type);
  if (/1ドリンク|1オーダー|ワンオーダー/.test(kind)) return '1ドリンク別';
  return kind || '飲み物条件は公式料金表で確認';
}

function slotPlan(plan, period, solo = false) {
  const prefix = solo ? 'solo_' : '';
  const days = period.days.map(({ key, day }) => ({
    day,
    prices: prices(oneRow(plan, `${prefix}${key}`), `${key}.${solo ? 'solo' : 'regular'}`),
  }));
  return {
    name: '30分',
    hours: period.hours,
    last_entry: '',
    days,
    drink: drinkText(plan, period.drink),
  };
}

export function convertCotedazur(shop) {
  const plans = shop.price_plan;
  if (!Array.isArray(plans) || plans.length !== 1 || plans[0]?.label_name !== '通常料金') {
    throw new Error(`${shop.shop_code}: 料金期間が複数、または通常料金以外です`);
  }
  const plan = plans[0];
  if (plan.day_of_week_select !== true) throw new Error(`${shop.shop_code}: 曜日別料金の構成が変わりました`);
  const regular = [{
    band: '通常料金',
    customers: CUSTOMERS,
    plans: PERIODS.map((period) => slotPlan(plan, period)),
    note: plan.all_annotation_note ?? '',
  }];
  const solo = [{
    band: '通常料金',
    customers: CUSTOMERS,
    plans: PERIODS.map((period) => slotPlan(plan, period, true)),
    note: plan.all_annotation_note ?? '',
  }];
  return { regular, solo };
}
