// ビッグエコーの公式料金表 (parseBigEcho の sections) を engine.js の regular / solo の形へ変換する。DOM に依存しない。
// 誤った金額を出さないことを最優先にする。確実に読めない表は使わず、店ごと読めなければ公式の料金表へ案内する。
// 対象は 30 分料金だけ。フリータイムとパックは表記の例外が多く、条件を取り違えやすいので計算しない。

const WEEK = ['月', '火', '水', '木', '金', '土', '日'];
const ALL_DAYS = [...WEEK, '祝', '祝前'];
const SLOT = 30;
const DAY = 1440;

const norm = (text) => (text ?? '').normalize('NFKC').replace(/\s+/g, '');

// ---- 曜日 ----

function addDayPart(days, part) {
  const range = /^([月火水木金土日])[~〜～-]([月火水木金土日])$/.exec(part);
  if (range) {
    let index = WEEK.indexOf(range[1]);
    const end = WEEK.indexOf(range[2]);
    days.add(WEEK[index]);
    while (index !== end) {
      index = (index + 1) % 7;
      days.add(WEEK[index]);
    }
    return true;
  }
  // 「土日祝日」のように区切りのない表記も読む
  const tokens = part.match(/祝前日?|祝祭日|祝日?|[月火水木金土日]/g);
  if (!tokens || tokens.join('') !== part) return false;
  for (const token of tokens) days.add(token.startsWith('祝前') ? '祝前' : token.startsWith('祝') ? '祝' : token);
  return true;
}

// 「【週末】金~日・祝前/祝」→ Set(金,土,日,祝,祝前)。曜日として読めなければ null
export function parseDays(label) {
  const text = norm(label).split('※')[0]
    .replace(/^【([^】]*)】$/, '$1')
    .replace(/【[^】]*】/g, '')
    .replace(/^(?:平日|週末)\((.+)\)$/, '$1')
    .replace(/\((?:平日|週末)\)/g, '')
    .replace(/\(祝(?:前)?日?除く\)/g, '')
    .replace(/曜日?/g, '');
  if (text === '全日' || text === '全日全時間帯') return new Set(ALL_DAYS);
  if (!text) return null;
  const days = new Set();
  return text.split(/[・,、/]/).every((part) => addDayPart(days, part)) && days.size ? days : null;
}

const labelOf = (days) => ALL_DAYS.filter((day) => days.has(day)).join('');
const isEveryWeekday = (days) => WEEK.every((day) => days.has(day));

// 曜日の列を engine の days に対応付ける。月曜の列を「平日」にすると祝前の判定を相方に任せられる
export function dayLabels(columns) {
  const union = new Set(columns.flatMap((days) => [...days]));
  if (columns.length === 1 && isEveryWeekday(union)) return { labels: ['全日'], partial: false, holidayUnknown: false };
  // 同じ曜日が 2 つの列にある表はどちらの料金か決められない（祝日と祝前は相方を優先するので重なってよい）
  if (WEEK.some((day) => columns.filter((days) => days.has(day)).length > 1)) return null;
  const complete = isEveryWeekday(union);
  const labels = columns.map((days) => (complete && days.has('月') ? '平日' : labelOf(days)));
  // 祝日の記載がない表は、日曜が平日側なら曜日どおりに読む。それ以外は祝日の料金を確定できないので出さない
  const holidayUnknown = !union.has('祝') && !columns.some((days) => days.has('月') && days.has('日'));
  // 一部の曜日だけの表は、残りの曜日に料金がないことを空の平日行で表す
  return { labels, partial: !complete, holidayUnknown };
}

// ---- 時間帯 ----

const RANGE = /(OPEN|\d{1,2}:\d{2})[~〜～-](?:翌朝?)?(CLOSE|LAST|\d{1,2}:\d{2})/i;
// 「OPEN〜18:00（土・日・祝08:00～）」の括弧は開店時刻の説明で、営業時間の表と同じことを言っている
const OPEN_NOTE = /^[(（※【]*[月火水木金土日祝前・]+(?:は)?朝?\d{1,2}:\d{2}(?:[~〜～]|から)[)）】]*$/;
// 「18:00〜CLOSE（金・土・祝前日～翌08:00）」は終わりの時刻だけの説明。短い方で読めば誤った金額は出ない
const CLOSE_NOTE = /^[(（※【]*[月火水木金土日祝前・]+(?:は)?(?:[~〜～](?:翌朝?)?|朝)\d{1,2}:\d{2}(?:まで)?[)）】]*$/;
const hourDigits = (text) => text.replace(/(\d{1,2})時(?!間)/g, '$1:00');

// 「【30分室料】OPEN〜17:59」→ { start: 'OPEN', end: '18:00' }。曜日で変わる時刻が併記された欄は読まない
export function parseTimeRange(text) {
  const t = hourDigits(norm(text));
  const match = RANGE.exec(t);
  if (!match) return null;
  const rest = t.replace(match[0], '').replace(/\(\)/g, '').replace(/^[^(（※【\d]*/, '');
  // 開店の説明は、時間帯の始まりが開店かその説明より遅いときだけ。早く始まる分は料金を出さないだけで済む
  if (/\d:\d/.test(rest) && !OPEN_NOTE.test(rest) && !CLOSE_NOTE.test(rest)) return null;
  const end = /:59$/.test(match[2]) ? `${Number(match[2].split(':')[0]) + 1}:00` : match[2].toUpperCase();
  return { start: match[1].toUpperCase(), end: end === 'LAST' ? 'CLOSE' : end };
}

const rangeText = ({ start, end }) => `${start === 'OPEN' ? '開店' : start}～${end === 'CLOSE' ? '閉店' : end}`;

// ---- 金額 ----

// 「891円(900円税込)」は税込の方を使う。形が違えば読めないものとして扱う
export function parseYen(cell) {
  const match = /^(\d+)円(?:\((\d+)円税込\))?$/.exec(norm(cell).replace(/,/g, ''));
  return match ? Number(match[2] ?? match[1]) : null;
}

// ---- 飲み物 ----

const DRINK_BAR = /(?:ドリンクバー|DB|ソフトドリンク)(?:飲み放題)?(?:料金|代|の料金)?(?:付|込|無料)/;
const ONE_DRINK = /(?:1|ワン)?ドリンク(?:オーダー|注文|制)|ワンオーダー|1オーダー/;
const STUDENT_BAR = /(?:学割|学生)[^。]*?(?:ドリンクバー|DB|1ドリンク無)/;
const SENIOR_BAR = /シニア[^。]*?(?:ドリンクバー|DB)/;

// 飲み物の条件。学割やシニアだけドリンクバー込みの店があるので区分ごとに返す
export function drinkOf(text, note = '') {
  const t = norm(text);
  const all = `${t} ${norm(note)}`;
  if (ONE_DRINK.test(t)) {
    return {
      drink: '1ドリンク別',
      student_drink: STUDENT_BAR.test(all) ? 'ドリンクバー付' : '',
      senior_drink: SENIOR_BAR.test(all) ? 'ドリンクバー付' : '',
    };
  }
  return { drink: DRINK_BAR.test(t) ? 'ドリンクバー付' : '', student_drink: '', senior_drink: '' };
}

// ---- 表の選別 ----

// 30 分料金でも条件付きの表は定価の比較に使わない
const NOT_REGULAR = /パック|フリー|FT|コース|プラン|倶楽部|クラブ|おはカラ|パスポート|学得|限定|特別期間|ダーツ|アミューズメント|ラグジュアリー|名様|\d時間|\d+月\d+/;

function isSlotSection(section) {
  if (!/30分/.test(norm(`${section.heading} ${section.time} ${section.title}`))) return false;
  return !NOT_REGULAR.test(norm(`${section.time} ${section.title}`));
}

const STUDENT_IS_MEMBER = /(?:学割|学生)[^。※]*?会員(?:様)?(?:価格|料金)/;
const SOLO_EXTRA = /(?:ひとり|一人|1人)カラオケ:?\+(\d+)円/;

function rowPrices(section, index, allStudents) {
  const prices = {};
  let solo = null;
  for (const row of section.rows) {
    const cell = norm(row[index + 1]);
    const value = parseYen(cell);
    const name = norm(row[0]);
    if (value === null) {
      if (cell && cell !== '-') return null;
      continue;
    }
    if (name === '会員' || name === '一般' || name === 'シニア') prices[name] = value;
    else if (name === '学割' || name === '学生') {
      prices.学生会員 = value;
      if (allStudents) prices.学生 = value;
    } else if (name.startsWith('ひとりカラオケ')) solo = value;
    else return null;
  }
  const extra = SOLO_EXTRA.exec(norm(section.header[index + 1]));
  if (extra && prices.会員 !== undefined) solo = prices.会員 + Number(extra[1]);
  return Object.keys(prices).length ? { prices, solo } : null;
}

// 表 1 つから { key, days(Set), prices, solo } の列を取り出す。読めなければ null
function readSection(section, allStudents) {
  const labels = section.header.slice(1);
  const range = parseTimeRange(section.time) ?? parseTimeRange(section.title);
  if (!labels.length || !range) return null;
  const columns = labels.map((label, index) => ({ index, label, days: parseDays(label) }));
  let picked = columns;
  let kind = '';
  if (columns.some((column) => !column.days)) {
    if (columns.some((column) => column.days)) return null; // 曜日と料金の種類が混ざった表
    // 「室料」「ドリンクバー付」のような料金の種類の列。飲み物なしの列を基準にする
    const hasPrice = (column) => section.rows.some((row) => parseYen(row[column.index + 1]) !== null);
    const room = columns.find((column) => /室料|30分料金|1ドリンク|ワンドリンク|ワンオーダー|1オーダー/.test(norm(column.label)) && hasPrice(column))
      ?? columns.find((column) => DRINK_BAR.test(norm(column.label)) && !/飲み放題[A-Z]?コース/.test(norm(column.label)) && hasPrice(column));
    if (!room) return null;
    kind = room.label;
    // 曜日は列の見出しにないので、表の題か時間帯の欄の「（月～金）」から読む
    const rest = hourDigits(norm(section.time)).replace(RANGE, '').replace(/[()]/g, '');
    const named = parseDays(norm(section.title).replace(/\(.*\)/, '')) ?? parseDays(rest);
    if (!named && /[月火水木金土日祝]/.test(`${norm(section.title)}${rest}`.replace(/\d+時間|日本|毎日/g, ''))) return null;
    picked = [{ ...room, days: named ?? new Set(ALL_DAYS) }];
  }
  const drinks = drinkOf(`${kind} ${section.time} ${section.title} ${section.heading}`, section.note);
  const hours = rangeText(range);
  const read = [];
  for (const column of picked) {
    const cells = rowPrices(section, column.index, allStudents);
    if (!cells) return null;
    read.push({ hours, drinks, days: column.days, ...cells });
  }
  return read;
}

// 同じ時間帯・同じ飲み物の条件の列を 1 つのプランにまとめる。曜日ごとに別の表になっている店があるため
function buildPlans(columns) {
  const groups = new Map();
  for (const column of columns) {
    const key = `${column.hours}|${column.drinks.drink}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(column);
  }
  const plans = [];
  const solos = [];
  for (const group of groups.values()) {
    const mapped = dayLabels(group.map((column) => column.days));
    if (!mapped) return null;
    const days = group.map((column, index) => ({ day: mapped.labels[index], prices: column.prices }));
    const soloDays = group.map((column, index) => ({
      day: mapped.labels[index],
      prices: column.solo === null ? {} : { 会員: column.solo },
    }));
    // 祝前日、祝日の順に曜日より優先する。engine と checkCoverage は先に当てはまった行を使う
    const rank = (row) => (row.day.includes('祝前') ? 0 : row.day.includes('祝') ? 1 : 2);
    const holidayFirst = (a, b) => rank(a) - rank(b);
    days.sort(holidayFirst);
    soloDays.sort(holidayFirst);
    if (mapped.partial) {
      days.push({ day: '平日', prices: {} });
      soloDays.push({ day: '平日', prices: {} });
    }
    if (mapped.holidayUnknown) {
      days.unshift({ day: '祝', prices: {} });
      soloDays.unshift({ day: '祝', prices: {} });
    }
    const { hours, drinks } = group[0];
    plans.push({ name: '30分', hours, last_entry: '', days, ...drinks });
    if (soloDays.some((row) => Object.keys(row.prices).length)) solos.push({ name: '30分', hours, last_entry: '', days: soloDays });
  }
  // 同じ時間帯に 1 ドリンクの表とドリンクバー付の表が並ぶ店は、同じ区分なら 1 ドリンクを基準にする
  for (const plan of plans) {
    if (plan.drink !== 'ドリンクバー付') continue;
    const base = plans.find((other) => other.drink === '1ドリンク別' && other.hours === plan.hours
      && other.days.map((row) => row.day).join() === plan.days.map((row) => row.day).join());
    if (!base) continue;
    plan.days.forEach((row, index) => {
      for (const customer of Object.keys(base.days[index].prices)) delete row.prices[customer];
    });
  }
  const hasPrices = (plan) => plan.days.some((row) => Object.keys(row.prices).length);
  return { plans: plans.filter(hasPrices), solos };
}

// ---- 店全体の検証 ----

const clock = (text) => {
  const [h, m] = text.split(':').map(Number);
  return h * 60 + m;
};

const planRange = (plan, open) => {
  const [startText, endText] = plan.hours.split('～');
  const from = startText === '開店' ? open[0] : clock(startText);
  let to = endText === '閉店' ? open[1] : clock(endText);
  if (startText === '開店' && to <= from) return null; // 18 時開店の日の「開店～18:00」は当てはまらない
  while (to <= from) to += DAY;
  return [from, to];
};

const rowFor = (plan, key) => {
  if (plan.days.length === 1) return plan.days[0];
  const tokens = (label) => label.match(/祝前|[月火水木金土日祝]/g) ?? [];
  return plan.days.find((row) => row.day !== '平日' && tokens(row.day).includes(key))
    ?? plan.days.find((row) => row.day === '平日');
};

// 同じ区分の同じコマに料金が 2 つある店と、営業時間の途中で料金が抜ける店は読み違いの可能性があるので使わない。
// 開店直後や閉店前だけの抜けは、その時間に金額を出さないだけなので許す
function checkCoverage(plans, week) {
  let covered = 0;
  for (const key of ALL_DAYS) {
    const open = key === '祝' ? week.日祝 : week[key];
    if (!open) continue; // 定休日
    const ranges = plans.map((plan) => [planRange(plan, open), rowFor(plan, key)?.prices ?? {}]);
    let state = 'before';
    for (let t = open[0]; t < open[1]; t += SLOT) {
      const seen = new Map();
      for (const [range, prices] of ranges) {
        if (!range || t < range[0] || t >= range[1]) continue;
        for (const customer in prices) {
          if (seen.has(customer) && seen.get(customer) !== prices[customer]) return 'overlap';
          seen.set(customer, prices[customer]);
        }
      }
      if (seen.size) {
        if (state === 'after') return 'gap';
        state = 'inside';
        covered += 1;
      } else if (state === 'inside') state = 'after';
    }
  }
  return covered ? null : 'gap';
}

// 「月曜日 9:00～2:00 ... 祝前日 9:00～5:00 日曜・祝祭日 9:00～2:00」→ { 月: [540, 1560], ... }。
// 閉店は開店より後の値にする。行のない日は定休日
export function parseWeekHours(text) {
  const week = {};
  const pattern = /(月|火|水|木|金|土|祝前|日曜・祝祭)(?:曜)?日?(\d{1,2}):(\d{2})[～〜~](\d{1,2}):(\d{2})/g;
  for (const match of norm(text).matchAll(pattern)) {
    const open = Number(match[2]) * 60 + Number(match[3]);
    let close = Number(match[4]) * 60 + Number(match[5]);
    while (close <= open) close += DAY;
    const key = match[1] === '日曜・祝祭' ? '日祝' : match[1];
    week[key] = [open, close];
  }
  if (week.日祝) week.日 = week.日祝;
  return WEEK.slice(0, 4).every((key) => week[key]) || Object.keys(week).length >= 5 ? week : null;
}

// { 月: [540, 1560], ... } → 「月火水木 9:00〜2:00 / 金土祝前 9:00〜5:00」。同じ時間の日をまとめる
export function weekHoursText(week) {
  const clockText = (minutes) => `${Math.floor((minutes % DAY) / 60)}:${String(minutes % 60).padStart(2, '0')}`;
  const groups = new Map();
  for (const key of ['月', '火', '水', '木', '金', '土', '祝前', '日祝']) {
    if (!week[key]) continue;
    const text = `${clockText(week[key][0])}〜${clockText(week[key][1])}`;
    groups.set(text, [...(groups.get(text) ?? []), key]);
  }
  if (groups.size === 1) return [...groups.keys()][0];
  return [...groups].map(([text, keys]) => `${keys.join('')} ${text}`).join(' / ');
}

const CUSTOMERS = ['学生会員', '学生', '会員', 'シニア', '一般'];
const ALERT_WORDS = /特別料金|特別期間|年末年始|お盆|GW|ゴールデンウィーク|繁忙/g;

// { regular, solo, week } か { reason } を返す
export function convertBigEcho(sections, hoursText) {
  const week = parseWeekHours(hoursText);
  if (!week) return { reason: 'no-hours' };
  const candidates = (sections ?? []).filter(isSlotSection);
  if (!candidates.length) return { reason: 'no-slot-table' };
  // 税別と書いてある表は、税込の金額が併記されたセルだけなら使える
  const taxFree = candidates.some((s) => /税[別抜]/.test(norm(`${s.heading} ${s.time} ${s.title} ${s.note} ${s.header.join(' ')}`))
    && s.rows.some((row) => row.slice(1).some((cell) => norm(cell) && !/税込/.test(norm(cell)))));
  if (taxFree) return { reason: 'tax-excluded' };
  const noteText = (sections ?? []).map((section) => norm(section.note)).join(' ');
  const allStudents = !STUDENT_IS_MEMBER.test(noteText);
  const columns = candidates.flatMap((section) => readSection(section, allStudents) ?? []);
  if (!columns.length) return { reason: 'unreadable-table' };
  const built = buildPlans(columns);
  if (!built) return { reason: 'unreadable-days' };
  const problem = checkCoverage(built.plans, week);
  if (problem) return { reason: problem };
  const table = (plans) => [{ band: '30分', customers: CUSTOMERS, plans, note: '' }];
  const alerts = [...new Set(candidates.flatMap((s) => norm(`${s.title} ${s.note}`).match(ALERT_WORDS) ?? []))];
  return { regular: table(built.plans), solo: built.solos.length ? table(built.solos) : [], week, alerts };
}
