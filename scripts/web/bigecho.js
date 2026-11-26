// ビッグエコーの公式料金表 (parseBigEcho の sections) を engine.js の regular / solo の形へ変換する。DOM に依存しない。
// 誤った金額を出さないことを最優先にする。確実に読めない表は使わず、店ごと読めなければ公式の料金表へ案内する。
// 30 分料金に加え、名前と時間帯と最終受付を確かめられたフリータイムとパックも読む。条件付きのプランは読まない。
// 料金が変わる期間の告知（お知らせ・表のない見出し・表の注記・日付の付いた表）が今日から 90 日先までと重なる店と、
// 30 分表の注記にある 1 人の追加料金を読めない店は、店ごと出さない。

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

// 特別な時期の料金を示す語。この語が表の名前にある表は、通常の料金として読まない
const SPECIAL_LABEL = /特別期間|特別料金|年末|年始|繁忙期|お盆|夏季|冬季|夏期|冬期|ゴールデンウィーク|GW|クリスマス|正月|大晦日/;
// 表の名前の中の日付（「12/1～」）。「2026/12/01」の一部は拾わない
const SLASH_DATE = /(?<![\d/])\d{1,2}\/\d{1,2}(?![\d/])/;
// 30 分料金でも条件付きの表は定価の比較に使わない
const NOT_REGULAR = /パック|フリー|FT|コース|プラン|倶楽部|クラブ|おはカラ|パスポート|学得|限定|ダーツ|アミューズメント|ラグジュアリー|名様|\d時間|\d+月\d+/;
// 見出し（h3）が特別な期間の料金を示す表も使わない
const SPECIAL_HEADING = /特別料金|期間限定/;

function isSlotSection(section) {
  if (!/30分/.test(norm(`${section.heading} ${section.time} ${section.title}`))) return false;
  const label = norm(`${section.time} ${section.title}`);
  if (NOT_REGULAR.test(label) || SPECIAL_LABEL.test(label) || SLASH_DATE.test(label)) return false;
  // 見出し（h3）や列見出しに日付のある表（「12/29～1/3の料金」）は、その期間だけの料金
  if (isDated(section)) return false;
  return !SPECIAL_LABEL.test(norm(section.heading)) && !SPECIAL_HEADING.test(norm(section.heading));
}

const STUDENT_IS_MEMBER = /(?:学割|学生)[^。※]*?会員(?:様)?(?:価格|料金)/;
const SOLO_EXTRA = /(?:ひとり|一人|1人)カラオケ:?\+(\d+)円/;

// ---- 30 分表の注記にある 1 人の追加料金 ----

// 1 人で使うときの説明。「お一人様30分+110円」（1 人あたりの料金）や「ひとりの料金+540円でドリンクバー」は含めない
const SOLO_CONTEXT = /ひとりカラオケ|ヒトカラ|(?:お?ひとり|お?一人|お?1人|1名)様?(?:での|で|の)?(?:ご)?(?:利用|の場合)/;
// はっきり読める形: 「30分毎に+50円」「30分+110円」「上記料金より+50円」「上記の30分料金+40円」「各種料金+40円」
const SOLO_CLEAR = /(?:30分(?:毎に|ごとに|毎|ごと|につき|あたり|当たり)?(?:料金)?|(?:上記|表記|各種)の?(?:価格|料金|金額)?(?:より|に|から)?(?:30分(?:毎に|ごとに|毎|ごと|につき|あたり)?)?(?:料金)?|(?:ひとり|一人|1人)カラオケ:?)\+(\d{1,3})円(?:\(税込\))?/;
// 30 分ごとの追加料金として読めない形（入室時の 1 回だけ、曜日で違う、税別）
const SOLO_UNCLEAR = /入室時|一律|1回|初回|最初|税[別抜]|[月火水木金土日祝](?:曜|[~〜・\-])|平日|土日|週末|祝日|祝前/;
const SOLO_PART = /[※*＊。◆■□●○〇◎◇☆★\n]/;
// 1 人で使うときの金額の書き方（「+50円」「1.2倍」「30分プラス25円」「別途料金」「【平日】330円」）。
// 「ひとりカラオケ料金は会員様料金となります」「店舗までお問い合わせください」は金額を書いていないので含めない
const SOLO_PRICE = /[+＋]\d|円|倍|プラス|別途|加算|割増|増し|増額/;
// 1 人の説明の後に続くドリンクバーや飲み放題の料金（「…ご案内ルーム数6部屋 ドリンクバーは別途660円」）は、1 人の料金ではない
const OTHER_SUBJECT = /(?:ドリンクバー|飲み放題|アルコール|ソフトドリンク)(?:は|を|も)/;
function soloPart(part) {
  const at = part.search(SOLO_CONTEXT);
  if (at < 0) return '';
  const cut = part.slice(at).search(OTHER_SUBJECT);
  return cut < 0 ? part : part.slice(0, at + cut);
}
const soloPriceParts = (texts) => texts.flatMap((text) => String(text ?? '').split(SOLO_PART))
  .map((part) => soloPart(norm(part))).filter((part) => part && SOLO_PRICE.test(part));

// 表 1 つの注記から 1 人の追加料金（30 分ごとの円）を読む。記載がなければ null、読めない形なら NaN
function soloNoteExtra(section) {
  const parts = soloPriceParts([section.heading, section.time, section.title, section.note]);
  if (!parts.length) return null;
  const values = new Set();
  for (const text of parts) {
    // 「30分プラス25円」は「30分+25円」と同じに読む
    const part = text.replace(/プラス(?=\d)/g, '+');
    const match = SOLO_CLEAR.exec(part);
    if (!match || (part.match(/\+\d+/g) ?? []).length !== 1 || SOLO_UNCLEAR.test(part.replace(/ひとりカラオケ/g, ''))) return NaN;
    values.add(Number(match[1]));
  }
  return values.size === 1 ? [...values][0] : NaN;
}

// 公式の表の誤記（「1,540円」を「154円」と書くなど、桁が 1 つ違う金額）。同じ列のほかの区分の金額の
// 10 倍か 10 分の 1 にほぼ一致し、列の中央の金額から大きく離れていたら、その列（その曜日の料金）を読まない
function hasDigitSlip(values) {
  if (values.length < 2) return false;
  const sorted = [...values].sort((a, b) => a - b);
  const middle = sorted[Math.floor(sorted.length / 2)];
  const near = (value, target) => Math.abs(value - target) <= target * 0.1;
  return values.some((value, index) => {
    const others = values.filter((_, other) => other !== index);
    return (value < middle * 0.4 && others.some((other) => near(value * 10, other)))
      || (value > middle * 2.5 && others.some((other) => near(value / 10, other)));
  });
}

function rowPrices(section, index, allStudents, soloExtra = null) {
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
  // 誤記のある列は金額を空にする。表ごと外すと同じ表のほかの曜日も消え、プランの並びがずれて
  // validate_data.py の前回との比べ方（表・プラン・曜日の番号）が別のプランの金額と比べてしまう
  if (hasDigitSlip([...Object.values(prices), ...(solo === null ? [] : [solo])])) return { prices: {}, solo: null };
  const extra = SOLO_EXTRA.exec(norm(section.header[index + 1]));
  if (extra && prices.会員 !== undefined) solo = prices.会員 + Number(extra[1]);
  // 注記にだけある「ひとりカラオケは 30 分ごとに +50 円」。見出しの追加料金と同じに会員の金額へ足す
  else if (solo === null && soloExtra !== null) {
    if (prices.会員 === undefined) return null;
    solo = prices.会員 + soloExtra;
  }
  return Object.keys(prices).length ? { prices, solo } : null;
}

// 表 1 つから { key, days(Set), prices, solo } の列を取り出す。読めなければ null
function readSection(section, allStudents, soloExtra = null) {
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
    const cells = rowPrices(section, column.index, allStudents, soloExtra);
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
    if (soloDays.some((row) => Object.keys(row.prices).length)) solos.push({ drink: drinks.drink, plan: { name: '30分', hours, last_entry: '', days: soloDays } });
  }
  // アプリは 1 人の料金を名前と時間帯で引くので、同じ時間帯に飲み物の条件違いの 1 人の料金が 2 つあると後の方が使われる。
  // 通常の料金と同じく 1 ドリンクの表を基準にし、なければ飲み物の条件のない表、最後にドリンクバー付の表を使う
  const DRINK_ORDER = ['1ドリンク別', '', 'ドリンクバー付'];
  const soloPlans = solos.filter((solo) => !solos.some((other) => other.plan.hours === solo.plan.hours
    && DRINK_ORDER.indexOf(other.drink) < DRINK_ORDER.indexOf(solo.drink))).map((solo) => solo.plan);
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
  return { plans: plans.filter(hasPrices), solos: soloPlans };
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
  // 「10:00～19:00の間の3時間」は時間帯の中で使える時間の決まったパック。フリータイムとして読むと長く居るほど安く見える
  if (/(?<![\d:])\d{1,3}(?:時間|分)|の間/.test(label)) return null;
  if (free) return `${free[1] ?? ''}フリータイム`;
  // 「フリータイム料金」の見出しの下の「OPEN〜19:00」。パックと並ぶ見出しではどちらか決められない
  if (/フリー/.test(heading) && !/パック/.test(heading)) return `${FREE_NAME.exec(heading)?.[1] ?? ''}フリータイム`;
  return null;
}

// 受付の文の区切り
const ENTRY_PART = /[※*【】()「」。◎◆■●☆★]/;
const DAY_BEFORE_ENTRY = /[月火水木金土日祝][^。※]*受付/;

// 最終受付。受付の記載があるのに時刻を読めない、時刻が 2 つある、曜日で変わる表は使わない
function lastEntryOf(section) {
  const text = clockDigits(norm(`${section.time} ${section.title} ${section.note}`));
  if (!/受付/.test(text)) return '';
  // 「受付時間 15:00～17:59」「受付時間15:00から18:00受付終了」は終わりの時刻が最終受付。「18:00～CLOSE」は閉店まで受け付ける
  const SPAN = /受付(?:時間)?[:：]?(\d{1,2}:\d{2})(?:[~〜～]|から|より)(\d{1,2}:\d{2}|CLOSE)/gi;
  const span = [...text.matchAll(SPAN)];
  if (span.length) {
    if (span.length !== 1 || DAY_BEFORE_ENTRY.test(text)) return null;
    return /close/i.test(span[0][2]) ? '' : span[0][2];
  }
  // 受付の文（※・【】・括弧・。などで区切った中）に違う時刻が 2 つあるのに範囲として読めなければ、どちらか決められない。
  // 「OPEN~20:00【受付17:59まで】」のように、時間帯と受付が別の区切りにあるものは読む
  const phrases = text.split(ENTRY_PART).filter((part) => /受付/.test(part))
    .map((part) => part.slice(part.search(/(?:最終)?受付/)));
  if (phrases.some((part) => new Set(part.match(/\d{1,2}:\d{2}/g) ?? []).size > 1)) return null;
  const times = [...new Set([...text.matchAll(LAST_ENTRY)].map((match) => match[1]))];
  if (times.length !== 1) return null;
  if (DAY_BEFORE_ENTRY.test(text)) return null;
  return times[0];
}

// ---- 税別 ----

const TAX_FREE = /税[別抜]/;
const sectionText = (section) => norm(`${section.heading} ${section.time} ${section.title} ${section.note} ${(section.header ?? []).join(' ')}`);
// 税込の金額が併記されていない金額のセルがある表
const hasUntaxedCell = (section) => section.rows.some((row) => row.slice(1).some((cell) => /\d/.test(norm(cell)) && !/税込/.test(norm(cell))));
// 税別と書いてある表は、税込の金額が併記されたセルだけなら使える。ほかの表の注記や表のない見出しに
// 「表示価格は税抜」とある店は、どの表も同じに扱う
const taxExcluded = (section, shopTax) => (shopTax || TAX_FREE.test(sectionText(section))) && hasUntaxedCell(section);

// 表 1 つを { columns } か { reason } にする
function readFixed(section, allStudents, shopTax = false) {
  const label = norm(`${section.heading} ${section.time} ${section.title}`);
  if (/30分/.test(label)) return { reason: 'slot' };
  if (NOT_FIXED.test(label) || SPECIAL_LABEL.test(label) || SLASH_DATE.test(label) || isDated(section) || NOT_FIXED_NOTE.test(norm(section.note))) {
    return { reason: 'condition' };
  }
  if (taxExcluded(section, shopTax)) return { reason: 'tax-excluded' };
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
  // 1 人の追加料金が注記だけにある表は、1 人の金額を出せない（通常の金額で出すと安く見える）。
  // 「+」のない形（「1.2倍」「プラス200円」「別途料金」）も 30 分表と同じに見る
  const soloNote = /(?:お?(?:一人|1人|ひとり)(?:様|カラオケ)?|1名様?)[^。]*?[+＋]\d/.test(norm(`${section.title} ${section.note} ${section.header.join(' ')}`))
    || soloPriceParts([section.title, section.note, ...section.header]).length > 0;
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
export function fixedReasons(sections, shopTax = false) {
  return (sections ?? []).map((section) => readFixed(section, true, shopTax).reason ?? 'ok');
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
  // 誤記ですべての列の金額を空にしたプランは配らない
  const hasPrices = (plan) => plan.days.some((row) => Object.keys(row.prices).length);
  return { plans: plans.filter((plan) => !twice(plan) && hasPrices(plan)), solos: solos.filter((plan) => !twice(plan)) };
}

// ---- 料金が変わる期間の告知 ----
//
// 店舗からのお知らせ、料金欄の表のない見出し、表の見出し・題・注記・列見出しに「12/1～1/5は特別料金」「2026年12月1日より
// 一部の料金を改定」のような告知があり、その期間が今日から WINDOW_DAYS 日先までと重なる店は金額を出さない。
// 利用者はアプリで過去でなければ何か月先の日付も選べるので、期間がまだ終わっていなければ、始まりが先でも出さない
// （ほかのチェーンと同じ。年のない日付は今日に近い回として読むので、1 年先までを見れば足りる）。
// 終わりの日付が読めない告知（「12月1日より」）も出さない。日付のない一般的な断り書き（「年末年始などは料金が異なる場合が
// ございます」）では止めない。

export const WINDOW_DAYS = 366;
// 料金の告知でない文にある、意味の分からない 1 日だけの日付（チケットの配布日など）は、配信データを使う 21 日先までだけ見る
const NEAR_DAYS = 21;
const DAY_MS = 86400000;
// 段落（お知らせの dd、見出し 1 つ、表 1 つ）の中の文の区切り
const SENTENCE = /[\n。※★☆◆■□●○〇◎◇▼▲]/;
const PRICE_WORD = /料金|価格|円|コース|プラン|室料/;
// 料金が変わることを表す語。「期間限定」は「期間限定料金」のときだけ。「11月末までの期間限定プラン」は通常の料金を変えない販促
const CHANGE_WORD = /特別|異な|変更|変わ|改定|改訂|改正|UP|アップ|値上|割増|増額|週末料金|(?:期間|時間)限定(?:の)?(?:料金|価格)|年末|年始|繁忙|問い?合わ?せ|問合せ|新(?:料金|価格)|(?:料金|価格)(?:体系)?(?:を|が|も)?(?:見直|新しく)|クリスマス料金|[+＋]\d+円(?:と|に)な/i;
// 日付のある文でだけ料金の告知と見なす語。「+200円でポテト食べ放題」「祝前日は土日祝料金」「フリータイムは実施しておりません」は、
// 日付がなければいつもの説明のことが多い。日付があれば、その日の上乗せ、ほかの曜日の料金、プランや割引を使えないこと、
// 受付の変更として読む
const DATED_CHANGE = /[+＋]\d|プラス\d|(?:休日|土日祝?日?|祝日|祝祭日|深夜)料金|実施(?:して|し|致し|いたし)?(?:おりません|ません)|(?:ござい|御座い)ません|休止|中止|停止|対象外|(?:利用|使用)(?:いただけ|頂け|でき|出来)(?:ません|ない)|不可|適用|受付.{0,8}(?:まで|変更|短縮)/;
const PLAN_WORD = /フリータイム|フリー|パック|割引|学割|シニア|会員|ひとりカラオケ|30分/;
// 誕生日の範囲（「平成20年(2008年)4月2日～…生まれ」）は料金の期間ではない
const NOT_PERIOD = /生まれ|生年月日/;
// 「場合がございます」のような、日付のない一般的な断り書き
const MAYBE = /場合|ことが(?:あり|ござい)|可能性/;
// 特別な時期の料金の表や見出しの名前（「年末年始料金」）。「学生特別料金」「会員特別価格」のような割引は含めない
const SEASON_LABEL = /特別期間|(?<!学生|学割|会員|会員様)特別(?:料金|価格)|年末|年始|繁忙期|お盆|夏季|冬季|夏期|冬期|ゴールデンウィーク|GW|クリスマス|正月|大晦日|元旦|元日/;
// 日付。年のある形を先に試す（「2026/12/01」を「2026/12」と「12/01」に分けて読まない）。
// 「期間：12月1日」のようなラベルのコロンの後は読み、「18:00」のような時刻の続きは読まない。
// 「12.29」も読むが、「1.2倍」「3.5時間」「2/3名様」のような数は読まない
const DAY_PART = '(\\d{1,2})(?:日|(?![\\d時:分円名歳人/月]))';
const NOT_AFTER_NUMBER = '(?<![\\d/.])(?<!\\d:)';
const NOT_QUANTITY = '(?![\\d/:時分円名歳人月倍%]|\\.\\d|ch|h)';
const DATE = new RegExp([
  `(20\\d{2})[/.\\-](\\d{1,2})(?:[/.\\-]${DAY_PART})?(?![\\d/.])`,
  `(20\\d{2})年(\\d{1,2})月(?:${DAY_PART})?`,
  `${NOT_AFTER_NUMBER}(\\d{1,2})月(?:${DAY_PART})?`,
  `${NOT_AFTER_NUMBER}(\\d{1,2})[/.](\\d{1,2})${NOT_QUANTITY}`,
].join('|'), 'g');
const WEEKDAY_OR_TIME = '(?:\\([^)]{1,6}\\))?(?:\\d{1,2}:\\d{2})?';
const RANGE_SEP = new RegExp(`^${WEEKDAY_OR_TIME}(?:[~〜\\-‐–—ー]|から|より)(?:翌日?)?$`);
// 「12/29・30・31」の日だけの続き、「12/28～31日」の日だけの終わり
const MORE_DAYS = new RegExp(`${WEEKDAY_OR_TIME}[・,、]${DAY_PART}`, 'y');
const DAY_END = new RegExp(`${WEEKDAY_OR_TIME}(?:[~〜\\-ー]|から)${DAY_PART}`, 'y');
const OPEN_AFTER = new RegExp(`^${WEEKDAY_OR_TIME}(?:より|から|以降|以後|[~〜\\-ー](?!\\d))`);
const END_AFTER = new RegExp(`^${WEEKDAY_OR_TIME}(?:まで|迄)`);
// 期間を示す行（「期間：12月1日～」「12/29(火)～1/3(日)」「12/29・30・31」）。料金の語がなくても「より」「まで」を読む
const PERIOD_LINE = /期間|期日|日程|日時|実施日|対象日/;
const PERIOD_FILLER = /\([^)]{1,6}\)|\d{1,2}:\d{2}|\d{1,2}日?|[~〜\-‐–—ー・,、:【】「」()[\]]|から|より|まで|迄|以降|以後|翌日?/g;

const dayNumber = (y, m, d) => Date.UTC(y, m - 1, d) / DAY_MS;
const lastDayOf = (y, m) => new Date(Date.UTC(y, m, 0)).getUTCDate();

// 'YYYY-MM-DD' か省略（日本時間の今日）を日の番号にする
export function dayOf(today) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(today ?? ''));
  if (match) return dayNumber(Number(match[1]), Number(match[2]), Number(match[3]));
  return Math.floor((Date.now() + 9 * 3600 * 1000) / DAY_MS);
}

// 「令和8年」は「2026年」にして読む
const westernYear = (text) => text.replace(/令和(元|\d{1,2})年/g, (_, n) => `${2018 + (n === '元' ? 1 : Number(n))}年`);

function dateTokens(text) {
  const tokens = [];
  for (const match of text.matchAll(DATE)) {
    const [y, m, d] = match[1] ? [match[1], match[2], match[3]] : match[4] ? [match[4], match[5], match[6]]
      : match[7] ? [undefined, match[7], match[8]] : [undefined, match[9], match[10]];
    const token = { y: y && Number(y), m: Number(m), d: d === undefined ? null : Number(d),
      start: match.index, end: match.index + match[0].length };
    if (token.m < 1 || token.m > 12 || (token.d !== null && (token.d < 1 || token.d > 31))) continue;
    tokens.push(token);
    if (token.d === null) continue;
    MORE_DAYS.lastIndex = token.end;
    for (let more = MORE_DAYS.exec(text); more; more = MORE_DAYS.exec(text)) {
      tokens.push({ ...token, d: Number(more[1]), start: more.index, end: MORE_DAYS.lastIndex });
    }
  }
  return tokens.sort((a, b) => a.start - b.start);
}

const isPeriodLine = (text) => PERIOD_LINE.test(text) || text.replace(DATE, '').replace(PERIOD_FILLER, '').length <= 1;

// 文の中の期間を { kind: 'range' | 'open' | 'end' | 'single', a, b, near } で返す。
// direct でない文（料金の告知でない文）の「より」「まで」は、期間を示す行でなければその日 1 日として扱い、near を付ける
function periodItems(text, direct) {
  const strong = direct || isPeriodLine(text);
  const tokens = dateTokens(text);
  const items = [];
  for (let i = 0; i < tokens.length; i += 1) {
    const a = tokens[i];
    const b = tokens[i + 1];
    if (b && RANGE_SEP.test(text.slice(a.end, b.start))) {
      items.push({ kind: 'range', a, b });
      i += 1;
      continue;
    }
    DAY_END.lastIndex = a.end;
    const dayEnd = a.d !== null ? DAY_END.exec(text) : null;
    if (dayEnd) {
      const d = Number(dayEnd[1]);
      items.push({ kind: 'range', a, b: { y: undefined, m: d >= a.d ? a.m : (a.m % 12) + 1, d } });
      continue;
    }
    const after = text.slice(a.end, a.end + 16);
    const before = text.slice(Math.max(0, a.start - 1), a.start);
    const kind = OPEN_AFTER.test(after) ? 'open' : END_AFTER.test(after) || /[~〜]/.test(before) ? 'end' : 'single';
    items.push(strong ? { kind, a } : { kind: 'single', a, near: true });
  }
  return items;
}

// 年のない日付は、今日に一番近い回として読む。[始まり, 終わり]（日の番号。開いた側は ±Infinity）か null
function resolvePeriod(item, today) {
  const year = new Date(today * DAY_MS).getUTCFullYear();
  const at = (token, y, edge) => {
    const d = token.d ?? (edge === 'start' ? 1 : lastDayOf(y, token.m));
    return d > lastDayOf(y, token.m) ? null : dayNumber(y, token.m, d);
  };
  const options = [];
  if (item.kind === 'range') {
    const { a, b } = item;
    const years = a.y ? [a.y] : b.y ? [b.y, b.y - 1] : [year - 1, year, year + 1];
    for (const ya of years) {
      const start = at(a, ya, 'start');
      let end = start === null ? null : at(b, b.y ?? ya, 'end');
      if (end !== null && !b.y && end < start) end = at(b, ya + 1, 'end');
      if (start === null || end === null || end < start) continue;
      options.push([start, end]);
      if (!a.y && b.y) break; // 終わりの年だけある期間は、始まりを同じ年か前の年にする
    }
  } else {
    for (const y of item.a.y ? [item.a.y] : [year - 1, year, year + 1]) {
      const first = at(item.a, y, 'start');
      const last = at(item.a, y, 'end');
      if (first === null || last === null) continue;
      options.push(item.kind === 'open' ? [first, Infinity] : item.kind === 'end' ? [-Infinity, last] : [first, last]);
    }
  }
  const distance = ([start, end]) => {
    const from = Number.isFinite(start) ? start : end;
    const to = Number.isFinite(end) ? end : start;
    return from <= today && today <= to ? 0 : Math.min(Math.abs(from - today), Math.abs(to - today));
  };
  return options.sort((x, y) => distance(x) - distance(y))[0] ?? null;
}

const overlapsWindow = (item, today) => {
  const period = resolvePeriod(item, today);
  return period !== null && period[0] <= today + (item.near ? NEAR_DAYS : WINDOW_DAYS) && period[1] >= today;
};

const sentencesOf = (text) => String(text ?? '').split(SENTENCE).map((part) => westernYear(norm(part)))
  .filter((part) => part && !NOT_PERIOD.test(part));
// 「値上げ」「値下げ」はそれだけで料金の告知
const isPriceChange = (sentence) => (PRICE_WORD.test(sentence) && CHANGE_WORD.test(sentence)) || /値上|値下/.test(sentence);
const isDatedChange = (sentence) => isPriceChange(sentence)
  || ((PRICE_WORD.test(sentence) || PLAN_WORD.test(sentence)) && (CHANGE_WORD.test(sentence) || DATED_CHANGE.test(sentence)));
// 「年末年始は特別料金となります」のように、特別な時期の料金だと言い切った文
const isSeasonPrice = (sentence) => SEASON_LABEL.test(sentence) && /料金|価格/.test(sentence) && !MAYBE.test(sentence);
const sectionParagraph = (section) => [section.heading, section.time, section.title, section.note,
  (section.header ?? []).join(' ')].join('\n');

// 期間の日付が付いた料金表（見出しの「12/29～1/3の料金」、時間帯の欄の「(2026年11月19日までの料金表です)」、
// 列見出しの「7/21~9/4 全日」）。その期間には日付のない表の金額を使えないので、期間が重なれば店ごと出さない。
// 学生や人数の条件の付いたプランと、食べ放題のような料金表でないものの日付は見ない（その表はもともと読まない）
const TABLE_WORD = /30分|フリー|FT|パック|料金|室料|ルーム/;
const CONDITIONAL = /限定|名様|学生|学割|コース|ダーツ|放題/;
const labelsOf = (section) => [section.heading, section.time, section.title, ...(section.header ?? [])]
  .map((text) => westernYear(norm(text)));
// 見出し・時間帯・題・列見出しのどこかに日付がある表。通常の料金として読まない
const isDated = (section) => labelsOf(section).some((text) => dateTokens(text).length);
function tablePeriods(section) {
  const label = norm(`${section.heading} ${section.time} ${section.title}`);
  if (!TABLE_WORD.test(label) || CONDITIONAL.test(label)) return [];
  return labelsOf(section).flatMap((text) => periodItems(text, true));
}

// 料金が変わる期間の告知のうち、今日から WINDOW_DAYS 日先までと重なるものを 1 つ返す。なければ null
export function specialPeriod({ sections = [], notice = '', priceNotes = [] } = {}, today = dayOf()) {
  const paragraphs = [notice, ...(priceNotes ?? []), ...(sections ?? []).map(sectionParagraph)];
  // お知らせと表のない見出しは、そのとき出した告知。表の注記は、いつも載せている説明のことが多い
  const posted = 1 + (priceNotes ?? []).length;
  for (const [index, paragraph] of paragraphs.entries()) {
    const sentences = sentencesOf(paragraph);
    let around = null;
    for (const sentence of sentences) {
      const own = periodItems(sentence, true);
      if (own.length) {
        if (isDatedChange(sentence) && own.some((item) => overlapsWindow(item, today))) return sentence;
        continue;
      }
      if (!isPriceChange(sentence)) continue;
      // 日付が同じ文にない告知（「12/29～1/3」の次の行に「上記期間は特別料金」）は、同じ段落のほかの文の日付で判断する
      around ??= sentences.flatMap((other) => periodItems(other, isPriceChange(other)));
      if (around.some((item) => overlapsWindow(item, today))) return sentence;
      // 「年末年始は特別料金となります」のように特別な時期の料金と言い切ったのに、期間の日付がない（チケットの配布日のような
      // 意味の分からない日付しかない）告知は、いつの料金か分からないので出さない。表の注記の「お盆・特別期間は特別料金」
      // 「特別料金の為クーポンの利用はできません」は、いつも載せている説明やプランの説明なので止めない
      if (index < posted && isSeasonPrice(sentence) && around.every((item) => item.near)) return sentence;
    }
  }
  for (const section of sections ?? []) {
    if (tablePeriods(section).some((item) => overlapsWindow(item, today))) return labelsOf(section).join(' ');
  }
  // 特別な時期の料金の表や見出しなのに、期間の日付がどこにもない（いつの料金か分からない）
  const undated = (paragraph) => !sentencesOf(paragraph).some((sentence) => dateTokens(sentence).length);
  for (const note of priceNotes ?? []) {
    const text = norm(note);
    if (SEASON_LABEL.test(text) && PRICE_WORD.test(text) && !MAYBE.test(text) && undated(note)) return text;
  }
  for (const section of sections ?? []) {
    const label = norm(`${section.heading} ${section.time} ${section.title}`);
    if (SEASON_LABEL.test(label) && !MAYBE.test(label) && undated(sectionParagraph(section))) return label;
  }
  return null;
}

const CUSTOMERS = ['学生会員', '学生', '会員', 'シニア', '一般'];
const ALERT_WORDS = /特別料金|特別期間|年末年始|お盆|GW|ゴールデンウィーク|繁忙/g;

// { regular, solo, week, alerts } か { reason } を返す。
// options: notice（店舗からのお知らせ）、priceNotes（料金欄の表のない見出し）、today（'YYYY-MM-DD'。省略すると日本時間の今日）
export function convertBigEcho(sections, hoursText, { notice = '', priceNotes = [], today } = {}) {
  if (specialPeriod({ sections, notice, priceNotes }, dayOf(today))) return { reason: 'special-period' };
  const week = parseWeekHours(hoursText);
  if (!week) return { reason: 'no-hours' };
  const candidates = (sections ?? []).filter(isSlotSection);
  if (!candidates.length) return { reason: 'no-slot-table' };
  // 税別と書いてある表は、税込の金額が併記されたセルだけなら使える。どこかの表の説明や見出しに税別とある店は、
  // フリータイムとパックも含めてどの表も同じに扱う
  const shopTax = (sections ?? []).some((section) => TAX_FREE.test(norm(`${section.heading} ${section.time} ${section.title} ${section.note}`)))
    || (priceNotes ?? []).some((text) => TAX_FREE.test(norm(text)));
  if (candidates.some((section) => taxExcluded(section, shopTax))) return { reason: 'tax-excluded' };
  // 注記にだけある 1 人の追加料金。読めない形の店は、1 人の金額を安く出さないように店ごと出さない
  const soloExtras = candidates.map(soloNoteExtra);
  if (soloExtras.some(Number.isNaN)) return { reason: 'solo-note' };
  const noteText = (sections ?? []).map((section) => norm(section.note)).join(' ');
  const allStudents = !STUDENT_IS_MEMBER.test(noteText);
  const bySection = candidates.map((section, index) => readSection(section, allStudents, soloExtras[index]) ?? []);
  // 注記に 1 人の追加料金がある 30 分表と、1 人の金額が何も書いていない 30 分表（朝の表など）が同じ店にあると、
  // 書いていない時間帯の 1 人の金額が分からない（通常の金額で出すと追加料金のぶん安く見える）
  if (soloExtras.some((extra) => typeof extra === 'number')
    && bySection.some((cols, index) => soloExtras[index] === null && cols.length && cols.every((column) => column.solo === null))) {
    return { reason: 'solo-note' };
  }
  const columns = bySection.flat();
  if (!columns.length) return { reason: 'unreadable-table' };
  const built = buildPlans(columns);
  if (!built) return { reason: 'unreadable-days' };
  const problem = checkCoverage(built.plans, week);
  if (problem) return { reason: problem };
  const fixed = buildFixed((sections ?? []).flatMap((section) => readFixed(section, allStudents, shopTax).columns ?? []));
  const tables = (slot, free) => [
    { band: '30分', customers: CUSTOMERS, plans: slot, note: '' },
    ...(free.length ? [{ band: 'フリータイム', customers: CUSTOMERS, plans: free, note: '' }] : []),
  ];
  // 告知の語。お知らせと表のない見出しからは、料金の告知の文だけから拾う
  const noticeText = [notice, ...(priceNotes ?? [])].flatMap(sentencesOf).filter(isPriceChange).join(' ');
  const alerts = [...new Set([...candidates.flatMap((s) => norm(`${s.title} ${s.note}`).match(ALERT_WORDS) ?? []),
    ...(noticeText.match(ALERT_WORDS) ?? [])])];
  return {
    regular: tables(built.plans, fixed.plans),
    solo: built.solos.length || fixed.solos.length ? tables(built.solos, fixed.solos) : [],
    week,
    alerts,
  };
}
