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

// ---- フリータイムとパック ----

// 誰でも使える定価のプランだけを読む。人数、性別、学生、予約、時期の条件が付くものは比べられないので外す
const NOT_FIXED = /パーティー|コース|名様|予約|限定|学生|学得|なでしこ|女性|レディース|早割|遅割|早得|おはカラ|期間|\d+月\d+|ダーツ|飲み放題|食事|ランチ|ナイトパック|エンドレス|プラン|倶楽部|クラブ|パスポート/;
// 注記は「学割・シニアは会員価格」のような説明が多いので、利用の条件を書いたものだけで外す
const NOT_FIXED_NOTE = /\d名様(?:以上|から|~)|名様以上|限定プラン|会員[^。※]*限定|グループ限定|期間限定|予約制|事前予約|\d+月\d+日/;
// 「※金曜・土曜は23時～29時」のように曜日で時間帯が変わる表は、どの曜日の時間か決められない
const DAY_HOURS = /[月火水木金土日祝][^。※]*?\d{1,2}(?::\d{2}|時)/;
const STUDENT_ONLY_BAR = /(?:学割|学生)(?:料金)?は(?:ソフト)?ドリンクバー付/;
const PACK = /(?:(?<=\d:\d{2})|(?<!\d))(\d{1,2})時間パック/; // 「OPEN~19:003時間パック」の 19:00 の 00 を拾わない
const FREE_NAME = /(昼|夕方|夜|深夜|朝まで|ロング|ショート|ミドル)?(?:フリータイム|フリー|FT)/;
const LAST_ENTRY = /(?:最終受付(?:時間)?|受付(?:終了|時間)?)(?:は)?[:：]?(\d{1,2}:\d{2})(?:まで)?/g;
const MAX_FIXED_PRICE = 9000;
const clockDigits = (text) => hourDigits(text.replace(/(\d{1,2})時(\d{1,2})分/g, (_, h, m) => `${h}:${m.padStart(2, '0')}`));

function fixedName(section) {
  const label = norm(`${section.time} ${section.title}`);
  const heading = norm(section.heading);
  const pack = PACK.exec(label);
  const free = FREE_NAME.exec(label);
  if (pack && free) return null; // 1 つの表に両方の名前があると、どちらの料金か決められない
  if (pack) return `${pack[1]}時間パック`;
  if (free) return `${free[1] ?? ''}フリータイム`;
  // 「フリータイム料金」の見出しの下の「OPEN〜19:00」。パックと並ぶ見出しではどちらか決められない
  if (/フリー/.test(heading) && !/パック/.test(heading)) return `${FREE_NAME.exec(heading)?.[1] ?? ''}フリータイム`;
  return null;
}

// 最終受付。受付の記載があるのに時刻を読めない、時刻が 2 つある、曜日で変わる表は使わない
function lastEntryOf(section) {
  const text = clockDigits(norm(`${section.time} ${section.title} ${section.note}`));
  if (!/受付/.test(text)) return '';
  // 「受付時間 15:00～17:59」は終わりの時刻が最終受付。「18:00～CLOSE」は閉店まで受け付ける
  const span = [...text.matchAll(/受付(?:時間)?[:：]?(\d{1,2}:\d{2})[~〜～](\d{1,2}:\d{2}|CLOSE)/gi)];
  if (span.length) return span.length === 1 && !/[月火水木金土日祝][^。※]*受付/.test(text) ? (/close/i.test(span[0][2]) ? '' : span[0][2]) : null;
  const times = [...new Set([...text.matchAll(LAST_ENTRY)].map((match) => match[1]))];
  if (times.length !== 1) return null;
  if (/[月火水木金土日祝][^。※]*受付/.test(text)) return null;
  return times[0];
}

// 表 1 つを { columns } か { reason } にする
function readFixed(section, allStudents) {
  const label = norm(`${section.heading} ${section.time} ${section.title}`);
  if (/30分/.test(label)) return { reason: 'slot' };
  if (NOT_FIXED.test(label) || NOT_FIXED_NOTE.test(norm(section.note))) return { reason: 'condition' };
  const words = clockDigits(norm(`${section.time} ${section.title}`)).replace(/(?:最終)?受付[^※]*/g, '');
  if (DAY_HOURS.test(words)) return { reason: 'day-hours' };
  const name = fixedName(section);
  if (!name) return { reason: 'name' };
  const lastEntry = lastEntryOf(section);
  if (lastEntry === null) return { reason: 'last-entry' };
  // 時間帯の欄に添えた受付の説明とプラン名は、時間帯を読む前に外す（受付は lastEntryOf で読んだ）
  const time = clockDigits(norm(section.time)).replace(/[※*]?最終受付.*$/, '').replace(/[(（【][^)）】]*(?:受付|パック|フリー)[^)）】]*[)）】]/g, '');
  const columns = readSection({ ...section, time }, allStudents);
  if (!columns) return { reason: 'table' };
  // 1 人の追加料金が注記だけにある表は、1 人の金額を出せない（通常の金額で出すと安く見える）
  const soloNote = /(?:お?(?:一人|1人|ひとり)(?:様|カラオケ)?|1名様?)[^。]*?[+＋]\d/.test(norm(`${section.title} ${section.note} ${section.header.join(' ')}`));
  if (soloNote && columns.every((column) => column.solo === null)) return { reason: 'solo-note' };
  for (const column of columns) {
    const values = [...Object.values(column.prices), column.solo].filter((value) => value !== null);
    if (values.some((value) => value < 100 || value > MAX_FIXED_PRICE)) return { reason: 'price-range' };
  }
  // 「学割はドリンクバー付」だけなら、ほかの区分の飲み物の条件は書いていない
  const studentOnly = STUDENT_ONLY_BAR.test(norm(section.title)) && !/1ドリンク|ワンドリンク|オーダー制/.test(norm(`${section.time} ${section.title}`));
  const fix = (drinks) => (studentOnly ? { drink: '', student_drink: 'ドリンクバー付', senior_drink: '' } : drinks);
  return { columns: columns.map((column) => ({ ...column, drinks: fix(column.drinks), name, lastEntry })) };
}

// 読まなかった理由の集計（調査用）
export function fixedReasons(sections) {
  return (sections ?? []).map((section) => readFixed(section, true).reason ?? 'ok');
}

// 同じ名前、時間帯、飲み物の条件の列を 1 つのプランにまとめる。読めないまとまりは捨てる（30 分料金だけで比べる）
function buildFixed(columns) {
  const groups = new Map();
  for (const column of columns) {
    const key = `${column.name}|${column.hours}|${column.lastEntry}|${column.drinks.drink}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(column);
  }
  const plans = [];
  const solos = [];
  const rank = (row) => (row.day.includes('祝前') ? 0 : row.day.includes('祝') ? 1 : 2);
  for (const group of groups.values()) {
    const mapped = dayLabels(group.map((column) => column.days));
    if (!mapped || mapped.holidayUnknown) continue;
    const days = group.map((column, index) => ({ day: mapped.labels[index], prices: column.prices }))
      .sort((a, b) => rank(a) - rank(b));
    const soloDays = group.map((column, index) => ({ day: mapped.labels[index], prices: column.solo === null ? {} : { 会員: column.solo } }))
      .sort((a, b) => rank(a) - rank(b));
    if (mapped.partial) {
      days.push({ day: '平日', prices: {} });
      soloDays.push({ day: '平日', prices: {} });
    }
    const { name, hours, lastEntry, drinks } = group[0];
    plans.push({ name, hours, last_entry: lastEntry, days, ...drinks });
    if (soloDays.some((row) => Object.keys(row.prices).length)) solos.push({ name, hours, last_entry: lastEntry, days: soloDays });
  }
  // 同じ名前と時間帯のプランが飲み物の条件違いで 2 つあると、どちらが定価か決められない
  const twice = (plan) => plans.filter((other) => other.name === plan.name && other.hours === plan.hours).length > 1;
  return { plans: plans.filter((plan) => !twice(plan)), solos: solos.filter((plan) => !twice(plan)) };
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
  const fixed = buildFixed((sections ?? []).flatMap((section) => readFixed(section, allStudents).columns ?? []));
  const tables = (slot, free) => [
    { band: '30分', customers: CUSTOMERS, plans: slot, note: '' },
    ...(free.length ? [{ band: 'フリータイム', customers: CUSTOMERS, plans: free, note: '' }] : []),
  ];
  const alerts = [...new Set(candidates.flatMap((s) => norm(`${s.title} ${s.note}`).match(ALERT_WORDS) ?? []))];
  return {
    regular: tables(built.plans, fixed.plans),
    solo: built.solos.length || fixed.solos.length ? tables(built.solos, fixed.solos) : [],
    week,
    alerts,
  };
}
