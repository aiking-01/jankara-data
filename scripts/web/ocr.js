// OCR が書き出した HTML の料金表を engine.js の regular の形へ変換する。DOM に依存しない。
// OCR は数字をよく読むが、表の列を足したり行を重ねたり、注記を落としたりする。形が少しでも想定と違えば店ごと読まない。
// 2 回読んだ店は、2 回の結果が完全に一致したときだけ使う。
// 料金に掛かる条件（人数割引、ドリンクバーの注文が必須、期間の料金など）を読み落とさないよう、
// 表の見出しと表の外の文は、知っている形だけを許す。知らない文が 1 つでもあれば店ごと読まない。

import { dayLabels, parseDays } from './bigecho.js';

const CUSTOMERS = ['学生会員', '学生', '会員', 'シニア', '一般'];
const MAX_SLOT_PRICE = 1500;
const DAY_SECONDS = 86400;
// 画像が読んだものと同じだと最後に確かめてからこの日数を過ぎた店は配らない。画像が取れないまま古い金額を出し続けない
export const STALE_DAYS = 14;
// アプリの日付選びには上限がなく、利用者は何か月先の日付も選べる。期間の告知は、期間がすべて終わるまで読まない。
// 夜の帯は翌朝まで続くので、前日に終わった期間は今日の朝まで効いているとみなす
const OVERNIGHT_DAYS = 1;
// 2026-11-26 の OCR の実行（予定は 11-25 22:10 UTC）は、取れた画像の sha256 が読んだものと同じことを確かめていたが、
// 記録する仕組みがなかった。確かめた記録（latest_key）がまだない店は、そのときに確かめたとみなす（12-10 に切れる）。
// その回に画像を取れなかった 6 店は確かめていないので、この扱いをしない
const LEGACY_VERIFIED_AT = Date.UTC(2026, 10, 25, 22, 10) / 1000;
const LEGACY_UNCHECKED = new Set(['123', '167', '205', '24', '63', '84']);

const norm = (text) => (text ?? '').normalize('NFKC').replace(/\s+/g, '');
// OCR は星形の囲みの文字などを画像の説明（img の alt）に書くことがある。人数割引などの条件が入っていても読み落とさないよう、
// 画像は「〔画像:説明〕」の文として残す（説明のない画像も）。ほかのタグの title なども同じ。ソフトクリームの挿絵（DECORATION）の
// ほかは、表の外なら unknown-note、表の中ならセルの形が合わずに、店ごと読まない。タグは消さずに後ろへ足す（表の形を変えない）
const PICTURE = /<[a-z][^>]*?\s(?:alt|title|aria-label)=(?:"([^"]*)"|'([^']*)')[^>]*>|<img\b[^>]*>/gi;
const pictureOf = (html) => html.replace(PICTURE, (tag, double, single) => `${tag} 〔画像:${(double ?? single ?? '').replace(/[〔〕<>]/g, '')}〕 `);
const textOf = (html) => pictureOf(html).replace(/<br\s*\/?>/gi, ' ').replace(/<[^>]+>/g, '')
  .replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&nbsp;/g, ' ');
// 表の外の文。表のあった所は「|」で区切る
const outsideOf = (html) => norm(textOf(html.replace(/<table>[\s\S]*?<\/table>/g, '|')));

// <table> を行ごとのセルの配列にする。colspan と rowspan は見出しの判定に使わないので展開しない。
// セルの文字には、セルのタグ自身の title なども入れる（cell[0] はセルのタグごと）
export function tablesOf(html) {
  return [...html.matchAll(/<table>([\s\S]*?)<\/table>/g)].map((table) => [...table[1].matchAll(/<tr>([\s\S]*?)<\/tr>/g)]
    .map((row) => [...row[1].matchAll(/<(t[hd])([^>]*)>([\s\S]*?)<\/t[hd]>/g)]
      .map((cell) => ({ head: cell[1] === 'th', rowspan: /rowspan/.test(cell[2]), text: norm(textOf(cell[0])) }))));
}

const RANGE = /^(\d{1,2}:\d{2})~(\d{1,2}:\d{2})/;
const KAN_PRICE = /^¥(\d{1,3}(?:,\d{3})?)$/;
// 「¥50(税込¥55)」は税込の金額を使う。税込が本体の 1.1 倍と合わなければ読み違いなので読まない
const TAXED_PRICE = /^¥(\d{1,3}(?:,\d{3})?)\(税込¥(\d{1,3}(?:,\d{3})?)\)$/;
const KAN_CUSTOMER = {
  学生: ['学生会員', '学生'],
  '学生・シニア': ['学生会員', '学生', 'シニア'],
  学生シニア: ['学生会員', '学生', 'シニア'],
  シニア: ['シニア'],
  会員: ['会員'],
  '会員・シニア': ['会員', 'シニア'],
  一般: ['一般'],
};
// セルの文字で引くので、Object のもとからある名前（'constructor' など）に当たらないよう、ここに書いた区分だけを見る
const customerOf = (name) => (Object.hasOwn(KAN_CUSTOMER, name) ? KAN_CUSTOMER[name] : null);
// 曜日の欄は曜日の文字だけ（parseDays は「※」の後ろなどを読み飛ばすので、条件の付いた欄を先に外す）
const DAY_LABEL = /^(?:全日|[月火水木金土日祝前・~]+)$/;

// 「会員・一般のお客様は別途ドリンクバー¥680をご注文いただきます」は注文が必須で、表の金額だけでは安く見える
const REQUIRED_BAR = /別途(?:ドリンクバー|ソフトドリンク飲み放題)/;
// 「★9:00~11:00までは、ドリンクバーのみの営業となります」。この時間はカラオケの料金がない
const BAR_ONLY = /★?(\d{1,2}:\d{2})~(\d{1,2}:\d{2})まで(?:は)?、?ドリンクバーのみの営業となります。?/g;

// 表の見出しの飲み物の条件で、知っている形。括弧の付いた曜日や区分の条件（「土・日・祝ドリンクバー+¥660」など）や
// 人数割引（「3名様で25円引」）は許さない
const DRINK_HEAD = /^(?:ドリンクバー付|ソフトドリンク飲み放題|(?:ドリンクバー付)?1ドリンク制(?:or(?:ドリンクバー|ソフトドリンク飲み放題)(?:\+¥\d{3}(?:\(税込¥\d{3}\))?|付))?)$/;

// 表の外の文で、30 分料金に掛からないと分かっている形。消してから残りを見る
const YEN = String.raw`¥\d{3}(?:\(税込¥\d{3}\))?`;
const DAYS_WORD = '(?:月~金|月~木|日~木・祝|金・土・祝前日|金・土・日|土・日・祝)';
// 中学生・高校生の室料 ¥0 の飲み物の条件。「[但し1ドリンク制、もしくは月~金+¥680、土・日・祝+¥770でドリンクバー付]」
const STUDENT_DRINK = String.raw`但し、?(?:1ドリンク制、もしくは)?(?:${DAYS_WORD}?\+?${YEN}、?)+で(?:ドリンクバー|ソフトドリンク飲み放題)`
  + String.raw`(?:付?をご注文いただきます。?|付?・ソフトクリーム(?:食べ放題)?付|付?&ソフトクリーム付|付き?)?`;
const STUDENT_CONDITION = String.raw`(?:[\[【]?${STUDENT_DRINK}[\]】]?`
  + String.raw`|但し、?月~金は1ドリンク制、もしくは\+${YEN}でドリンクバー付をご注文、土・日・祝\+${YEN}でドリンクバー付をご注文いただきます。?`
  + String.raw`|(?:${DAYS_WORD}\+${YEN}、?)+でドリンクバー付)`;
// 中学生・高校生は室料 ¥0（アプリの区分にはない）。この注記に続く飲み物の条件、対象人数、実施時間の括弧も一緒に消す。
// 「室料」は OCR が「堂料」「宝料」などと読むことがある
const STUDENT_NOTE = new RegExp(String.raw`★?中学生は\d{1,2}時、高校生は\d{1,2}時まで(?:\(土・日・祝は\d{1,2}時まで\))?、?`
  + String.raw`(?:月~[金土])?(?:[室堂宝全会生料有実]料|料金)¥?0(?:\(土・日・祝は2名様以上対象\))?(?:${STUDENT_CONDITION})?`
  + String.raw`(?:\(2名様以上対象\))?(?:\(土・日・祝の実施時間は(?:OPEN~\d{1,2}時\/)?\d{1,2}時~\d{1,2}時とさせて頂きます。?`
  + String.raw`(?:[^()★|]{1,20}店は、実施時間に制限はございません。)?\))?`, 'g');
// モーニングパック（別の定額のプラン。30 分料金は変わらない）
const MORNING_NOTE = new RegExp(String.raw`(?:モーニングパック実施中!!)?(?:オープン~12時の間で最大3時間まで利用可能)?`
  + String.raw`※他のサービスとの併用はできません。OPEN~12:00※最終受付11:00カラオケ\+ドリンクバー`
  + String.raw`(?:平日|土日・?祝|\d{3}(?:円\(税込\)|\(税込\)円|円))*\+350(?:円\(税込\)|\(税込\)円)でモーニングメニュー1品!!`, 'g');
const KNOWN_NOTES = [
  // 見出し「~お1人様30分税込料金~」
  /~?お1[人名]様30分(?:税込)?(?:料金)?~?/g,
  // 混雑時の利用制限
  /★?混雑時はご利用(?:時間)?を制限(?:させていただく場合がございます|、?又は?(?:ご利用を|ご制限を|お制限を)?お断りさせて(?:頂く場合がございます|いただく場合がございます|頂きます))。?/g,
  STUDENT_NOTE,
  MORNING_NOTE,
  // ダーツ（カラオケの料金ではない）
  /★?ダーツのご利用は、?投げ放題で(?:上記同一料金|30分料金は上記同一|上記料金\+¥\d{2,3})となります。?/g,
  // ソフトクリーム
  /★?ドリンクバー選択の(?:学生の)?お客様はソフトクリーム食べ放題となります。?/g,
  /ドリンクバー飲み放題利用でソフトクリーム\+120円で食べ放題!!/g,
];
// ソフトクリームの挿絵の説明（「Soft serve ice cream cone illustration」「Illustration of a swirl of soft-serve ice cream
// on a cone」など。空白は norm で消えている）。説明に文字や金額があるものは当たらない。
// 挿絵は文の途中（「ドリンクバー飲み放題利用で〔画像〕ソフトクリーム+120円で…」）に入るので、ほかの文より先に消す
const DECORATION = /〔画像:(?:illustrationof)?a?(?:swirlof)?(?:soft-?serve|swirl)?icecream(?:cone)?(?:onacone)?(?:illustration)?〕/gi;
// 消した後に残ってよい文字（区切りだけ）
const LEFTOVER = /^[|★、。・※]*$/;

// 期間の告知。年末年始などの言葉か日付のある文は、期間（始めと終わりの日付）が読めて、
// その期間がもう終わっているときだけ、その文を消して残りを読む
const PERIOD_WORD = /年末年始|年始|年末|特別料金|特別営業|料金改定|改定|期間限定|期間中|お盆|ゴールデンウ[ィイ]ーク|GW|臨時/;
const DATE_WORD = /\d{1,2}(?:\/|月)\d{1,2}|\d{4}年/;
const DATE = String.raw`(?:(\d{4})[年/])?(\d{1,2})(?:\/|月)(\d{1,2})(?![\d:])日?(?:\([月火水木金土日祝・]+\))?`;
// 終わりの月は省かれることがある（「12月28日~31日」）。「1/5~12:00」の時刻は日付と読まない
const END_DATE = String.raw`(?:(\d{4})[年/])?(?:(\d{1,2})(?:\/|月))?(\d{1,2})(?![\d:])日?(?:\([月火水木金土日祝・]+\))?`;
const PERIOD = new RegExp(String.raw`${DATE}(?:~|〜|から)${END_DATE}`, 'g');

// カラオケ館の 1 つの表。1 行目に時間帯と飲み物の条件、2 行目に区分、その下に曜日ごとの金額が並ぶ
// 飲み物の条件は、学生の列だけに掛かる見出しを OCR が全部の列に広げることがあり、2 回とも同じ誤りになるので使わない。
// 店と時間帯と区分で「ドリンクバー付」と「1ドリンク制」が分かれるので、込みと誤解されないよう公式の表へ案内する
const UNKNOWN_DRINK = 'ドリンク条件は公式の表で確認';
const isDrinkHead = (text) => text === '' || DRINK_HEAD.test(text);

function priceOf(text) {
  const plain = KAN_PRICE.exec(text);
  if (plain) return Number(plain[1].replace(',', ''));
  const taxed = TAXED_PRICE.exec(text);
  if (!taxed) return null;
  const base = Number(taxed[1].replace(',', ''));
  const total = Number(taxed[2].replace(',', ''));
  return Math.abs(total - base * 1.1) < 1 ? total : null;
}

// 「月~金」と「祝前日」が 2 行に分かれて「月~金祝前日」と読まれる
const dayText = (text) => text.replace(/^([月火水木金土日]~[月火水木金土日])(祝前日?|祝日?)$/, '$1・$2');
const daysOf = (text) => (DAY_LABEL.test(text ?? '') ? parseDays(dayText(text)) : null);

function readKanTable(rows) {
  let [first, second, ...body] = rows;
  if (!first || !second) return { reason: 'table-shape' };
  let rangeCell = first[0];
  let heads = first.slice(1); // 区分ではない見出し（飲み物の条件）
  let headRow = second;
  // 時間帯が 2 行目の先頭に入り、1 行目が飲み物の見出しだけになる読み方がある
  if (!RANGE.test(first[0]?.text ?? '') && RANGE.test(second[0]?.text ?? '') && !first.some((cell) => RANGE.test(cell.text))) {
    rangeCell = second[0];
    heads = first;
    headRow = [{ text: '' }, ...second.slice(1)];
  }
  const range = RANGE.exec(rangeCell?.text ?? '');
  if (!range) return { reason: 'no-range' };
  if (narrowed(range[1], range[2], rangeCell.text.slice(range[0].length))) return { reason: 'band-note' };
  // ドリンクバーの注文が必須の表は、時間帯だけを読んで外す（どの表の注記かは readKan で確かめる）
  if (rows.some((row) => row.some((cell) => REQUIRED_BAR.test(cell.text)))) {
    return { requiredBar: true, start: range[1], end: range[2] };
  }
  let headless = false;
  // 飲み物の見出しがなく、1 行目に時間帯と区分が並ぶ表。必須のドリンクバーの見出しを落とした読み方と区別できないので、
  // どの読み取りにもその注記がないことを convertKanOcr で確かめる
  if (headRow === second && first.length > 1 && first.slice(1).every((cell) => customerOf(cell.text))
    && daysOf(second[0]?.text)) {
    heads = [];
    headRow = [{ text: '' }, ...first.slice(1)];
    body = [second, ...body];
    headless = true;
  }
  if (!body.length) return { reason: 'table-shape' };
  // 曜日の列の上の空いた見出しと、1 行目からずれて入った飲み物の見出しは読み飛ばす。
  // 区分の見出しが途中で空いているときは列がずれているので読まない
  let skip = 0;
  while (skip < headRow.length && (headRow[skip].text === '' || /ドリンク/.test(headRow[skip].text))) skip += 1;
  heads = [...heads, ...headRow.slice(0, skip)];
  let customers = headRow.slice(skip).map((cell) => cell.text);
  if (!heads.every((cell) => isDrinkHead(cell.text))) return { reason: 'unknown-head' };
  // 飲み物の見出しごとに「学生 会員 一般」を 2 回繰り返す読み方がある。後ろ半分が空か前半と同じなら前半を使う
  const half = customers.length / 2;
  const texts = (cells) => cells.map((cell) => cell.text).join('|');
  if (Number.isInteger(half) && half > 1 && customers.slice(0, half).join() === customers.slice(half).join()
    && body.every((row) => row.length === customers.length + 1 && (row.slice(1 + half).every((cell) => cell.text === '')
      || texts(row.slice(1 + half)) === texts(row.slice(1, 1 + half))))) {
    customers = customers.slice(0, half);
    body = body.map((row) => row.slice(0, 1 + half));
  }
  // 「ドリンクバー付[学生] | 1ドリンク制[会員 一般]」の夜の表を、OCR が「学生|会員|学生|会員」と読むことがある。
  // 1 行目が時間帯と 2 つの飲み物の見出しで、金額が 3 つと空のセル 1 つのときだけ「学生 会員 一般」と読む。
  // 会員と一般の検算（下）が全行で合わなければ読まない
  if (!headless && headRow === second && first.length === 3 && customers.length === 4
    && customers[0] === customers[2] && /^学生/.test(customers[0]) && customers[1] === '会員' && customers[3] === '会員'
    && body.every((row) => row.length === 5 && row[4].text === '')) {
    customers = [customers[0], '会員', '一般'];
    body = body.map((row) => row.slice(0, 4));
  }
  if (!customers.length || !customers.every((name) => customerOf(name)) || new Set(customers).size !== customers.length) {
    return { reason: 'customer-head' };
  }
  // 会員と一般の列がない表は検算できないので読まない
  if (!customers.includes('一般') || !customers.some((name) => customerOf(name).includes('会員'))) return { reason: 'ratio' };
  const columns = [];
  for (const row of body) {
    const [label, ...cells] = row;
    const days = daysOf(label?.text);
    if (!days) return { reason: 'day-label' };
    if (cells.length !== customers.length) return { reason: 'price-count' };
    const prices = {};
    for (const [index, cell] of cells.entries()) {
      const value = priceOf(cell.text);
      if (value === null) return { reason: 'price-text' };
      if (value < 1 || value > MAX_SLOT_PRICE) return { reason: 'price-range' };
      for (const name of customerOf(customers[index])) prices[name] = value;
    }
    // 一般は会員の 4/3 倍（1 円未満切り捨て）。どの店のどの行もこの関係なので、合わなければ読み違い
    if (prices.一般 !== Math.floor(prices.会員 * 4 / 3)) return { reason: 'ratio' };
    columns.push({ days, prices });
  }
  const mapped = dayLabels(columns.map((column) => column.days));
  if (!mapped || mapped.partial || mapped.holidayUnknown) return { reason: 'days' };
  const days = columns.map((column, index) => ({ day: mapped.labels[index], prices: column.prices }));
  const plan = { name: '30分', hours: `${range[1]}～${range[2]}`, last_entry: '', days, drink: UNKNOWN_DRINK };
  return { plan, requiredBar: false, headless, start: range[1], end: range[2] };
}

const minutesOf = (clock) => clock.split(':').reduce((hours, minutes) => Number(hours) * 60 + Number(minutes));

function spanOf(start, end) {
  const from = minutesOf(start);
  let to = minutesOf(end);
  if (to <= from) to += 1440;
  return [from, to];
}

// 「★土・日・祝(OPEN~19:00)の、お一人様でのご利用は上記金額+¥50となります」。曜日と区分と時間帯は省かれることがある
const SOLO_NOTE = /★?(会員・一般)?\(?(土・?日・?祝)?[(:]?((?:O?OPEN|\d{1,2}:\d{2})~\d{1,2}:\d{2})?\)?の?、?(?:お一人様|ひとりカラオケ)(?:での?|の)ご利用は上記(?:金額|料金)\+¥(\d{1,3})となります。?/g;
const SOLO_WORD = /お一人様|ひとりカラオケ|お?1[人名]様での/;
const DARTS = /ダーツのご利用は[^★。]*?となります/g;

// 「18:00~2:00(金・祝前日~3:00)(土~4:00)」の括弧は曜日ごとの時刻。帯より広いだけなら、帯の中の金額は変わらない。
// 「9:00~11:00(土・日・祝~10:00)」のように狭い曜日があると、その曜日の残りは次の帯の金額になるので読まない
function narrowed(start, end, rest) {
  const [from, to] = spanOf(start, end);
  const notes = [...rest.matchAll(/\(([^()]*)\)/g)].map((match) => match[1]);
  if (rest.replace(/\([^()]*\)/g, '') !== '') return true;
  for (const note of notes) {
    const parts = [...note.matchAll(/(\d{1,2}:\d{2})?~(\d{1,2}:\d{2})?/g)];
    if (!parts.length || parts.some((part) => !part[1] && !part[2])) return true;
    for (const [, noteFrom, noteTo] of parts) {
      if (noteFrom && minutesOf(noteFrom) > from) return true;
      if (noteTo && spanOf(start, noteTo)[1] < to) return true;
    }
  }
  return false;
}

// 1 人で使うときの上乗せを書いた注記を読む。時間帯が表の区切りと合わない注記や、ほかの上乗せがあれば読まない
function soloNotes(text, spans) {
  const notes = [];
  for (const match of text.matchAll(SOLO_NOTE)) {
    const [, customers, days, window, extra] = match;
    if (!window) return { reason: 'solo-window' };
    const [from, to] = window.split('~');
    const start = /OPEN/.test(from) ? Math.min(...spans.map(([begin]) => begin)) : minutesOf(from);
    let end = minutesOf(to);
    if (end <= start) end += 1440;
    const inside = spans.map(([begin, finish]) => start <= begin && finish <= end);
    const touches = spans.map(([begin, finish]) => begin < end && start < finish);
    if (inside.some((value, index) => value !== touches[index]) || !inside.some(Boolean)) return { reason: 'solo-window' };
    notes.push({ inside, days: days ? '土日祝' : null, customers: customers ? ['会員', '一般'] : null, extra: Number(extra) });
  }
  const rest = text.replace(SOLO_NOTE, '').replace(DARTS, '');
  if (SOLO_WORD.test(rest) || /上記[^★。]*?\+¥?\d/.test(rest)) return { reason: 'surcharge-note' };
  return { notes };
}

// 通常の表に注記の上乗せを足して 1 人用の表を作る。曜日の行が注記の曜日と合わなければ読まない
function soloPlans(plans, notes) {
  const solo = plans.map((plan) => plan && ({ ...plan, days: plan.days.map((row) => ({ ...row, prices: { ...row.prices } })) }));
  for (const note of notes) {
    for (const [index, plan] of solo.entries()) {
      if (!note.inside[index] || !plan) continue;
      for (const row of plan.days) {
        if (note.days && row.day !== note.days) {
          if (row.day !== '平日') return null;
          continue;
        }
        for (const name of Object.keys(row.prices)) {
          if (!note.customers || note.customers.includes(name)) row.prices[name] += note.extra;
        }
      }
    }
  }
  return solo;
}

// 日付を 1970-01-01 からの日数にする。比べるだけなので時差は気にしない
const dayNumber = (year, month, day) => Date.UTC(year, month - 1, day) / (DAY_SECONDS * 1000);
const validDate = (year, month, day) => {
  const date = new Date(Date.UTC(year, month - 1, day));
  return date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day;
};

// 「12/28~1/4」の始めと終わりを日数にする。年のない日付は、今日に最も近い年の期間とみなし、
// 終わりが始めより前の月日なら終わりを翌年にする（年をまたぐ期間）。読めなければ null
export function periodDays(match, today) {
  const [, year1, month1, day1, year2, month2Text, day2] = match.map((value) => (value === undefined ? undefined : Number(value)));
  const month2 = month2Text ?? month1;
  const [thisYear, thisMonth, thisDay] = today.split('-').map(Number);
  const now = dayNumber(thisYear, thisMonth, thisDay);
  const candidates = [];
  for (const start of year1 ? [year1] : [thisYear - 1, thisYear, thisYear + 1]) {
    const end = year2 ?? (month2 * 100 + day2 < month1 * 100 + day1 ? start + 1 : start);
    if (!validDate(start, month1, day1) || !validDate(end, month2, day2)) continue;
    const period = [dayNumber(start, month1, day1), dayNumber(end, month2, day2)];
    if (period[0] <= period[1]) candidates.push(period);
  }
  if (!candidates.length) return null;
  const distance = ([from, to]) => (now < from ? from - now : now > to ? now - to : 0);
  return candidates.reduce((best, period) => (distance(period) < distance(best) ? period : best));
}

// 期間の告知を探す。期間がもう終わっていれば、その文を消した残りを返す。
// 期間がまだ終わっていない（何か月先でも）か、期間（終わりの日付）が読めなければ null（店ごと読まない）。
// 告知の期間だけを料金なしにする形はアプリにないので、店ごと「料金表を見る」にする
export function withoutPeriods(text, today) {
  const isPeriod = (sentence) => PERIOD_WORD.test(sentence) || DATE_WORD.test(sentence);
  if (!isPeriod(text)) return text;
  const [year, month, day] = today.split('-').map(Number);
  const now = dayNumber(year, month, day);
  const kept = [];
  for (const sentence of text.split(/(?=★)|(?<=[|。])/)) {
    if (!isPeriod(sentence)) {
      kept.push(sentence);
      continue;
    }
    const periods = [...sentence.matchAll(PERIOD)].map((match) => periodDays(match, today));
    if (!periods.length || periods.some((period) => !period)) return null;
    if (periods.some(([, to]) => now <= to + OVERNIGHT_DAYS)) return null;
    // 期間の外の告知。日付のほかに期間の言葉のない日付が残っていれば、読めていない期間がある
    if (DATE_WORD.test(sentence.replace(PERIOD, ''))) return null;
  }
  return kept.join('');
}

// 表の外の文から、知っている形の文を消した残り。残りがあれば料金に掛かる条件かもしれない
function unknownNote(outside, today) {
  let rest = withoutPeriods(outside.replace(DECORATION, ''), today);
  if (rest === null) return { reason: 'period' };
  rest = rest.replace(SOLO_NOTE, '').replace(BAR_ONLY, '');
  for (const pattern of KNOWN_NOTES) rest = rest.replace(pattern, '');
  return LEFTOVER.test(rest) ? null : { reason: 'unknown-note', rest };
}

// 1 回分の読み取りを regular と solo にする
function readKan(html, today) {
  const text = norm(textOf(html));
  if (!/30分/.test(text)) return { reason: 'not-30min' };
  if (/税[別抜]/.test(text)) return { reason: 'tax-excluded' };
  const tables = tablesOf(html);
  if (!tables.length) return { reason: 'no-table' };
  const outside = outsideOf(html);
  // ドリンクバーの注文が必須の注記が表の外にあると、どの時間帯の注記か分からない
  if (REQUIRED_BAR.test(outside)) return { reason: 'required-bar' };
  const plans = [];
  const spans = [];
  const requiredBar = [];
  let headless = false;
  for (const rows of tables) {
    const table = readKanTable(rows);
    if (table.reason) return table;
    headless ||= table.headless;
    const span = spanOf(table.start, table.end);
    if (spans.some(([from, to]) => span[0] < to && from < span[1])) return { reason: 'band-overlap' };
    spans.push(span);
    plans.push(table.plan ?? null);
    requiredBar.push(table.requiredBar);
  }
  // ドリンクバーが必須の表は朝の帯にしかない。後ろの帯の表に付いていたら、注記が隣の表に移った読み違いかもしれない
  const barStarts = spans.filter((_, index) => requiredBar[index]).map(([from]) => from);
  const keptStarts = spans.filter((_, index) => !requiredBar[index]).map(([from]) => from);
  if (barStarts.length && keptStarts.length && Math.max(...barStarts) > Math.min(...keptStarts)) return { reason: 'required-bar' };
  // 「ドリンクバーのみの営業」の時間は料金がない。帯の始めから途中までならその分を削り、それ以外は読まない
  for (const match of text.matchAll(BAR_ONLY)) {
    const [from, to] = spanOf(match[1], match[2]);
    for (const [index, plan] of plans.entries()) {
      if (!plan) continue;
      const [begin, finish] = spans[index];
      if (to <= begin || finish <= from) continue;
      if (from !== begin || to >= finish) return { reason: 'bar-only' };
      plans[index] = { ...plan, hours: `${match[2]}～${plan.hours.split('～')[1]}` };
      spans[index] = [to, finish];
    }
  }
  if (/ドリンクバーのみ/.test(text.replace(BAR_ONLY, ''))) return { reason: 'bar-only' };
  const { notes, reason } = soloNotes(text, spans);
  if (reason) return { reason };
  const solo = notes.length ? soloPlans(plans, notes) : [];
  if (!solo) return { reason: 'solo-days' };
  const kept = (list) => list.filter((plan, index) => plan && !requiredBar[index]);
  if (!kept(plans).length) return { reason: 'required-bar' };
  // 表の外に知らない文（人数割引、期間の料金、ほかの区分の条件など）があれば読まない
  const unknown = unknownNote(outside, today);
  if (unknown) return unknown;
  const table = (list) => [{ band: '30分', customers: CUSTOMERS, plans: kept(list), note: '' }];
  return { regular: table(plans), solo: solo.length ? table(solo) : [], headless };
}

// JST の今日（'2026-11-26'）。テストや手元では CONVERT_TODAY で差し替える
export function todayJst(env = process.env, now = Date.now()) {
  if (/^\d{4}-\d{2}-\d{2}$/.test(env.CONVERT_TODAY ?? '')) return env.CONVERT_TODAY;
  return new Date(now + 9 * 3600 * 1000).toISOString().slice(0, 10);
}

// 読み取りが 1 回なら確かめる手段がないので使わない。2 回とも同じ結果のときだけ返す
export function convertKanOcr(reads, { today = todayJst() } = {}) {
  if (!reads || reads.length < 2) return { reason: 'single-read' };
  const results = reads.map((html) => readKan(html, today));
  const failed = results.find((result) => result.reason);
  if (failed) return failed;
  // 飲み物の見出しのない表は、必須のドリンクバーの見出しを落とした読み方かもしれない。どれかの読み取りにその注記があれば読まない
  if (results.some((result) => result.headless) && reads.some((html) => REQUIRED_BAR.test(norm(textOf(html))))) {
    return { reason: 'required-bar' };
  }
  const [first, ...others] = results.map((result) => JSON.stringify([result.regular, result.solo]));
  if (others.some((other) => other !== first)) return { reason: 'reads-differ' };
  return { regular: results[0].regular, solo: results[0].solo };
}

const timeOf = (value) => (Number.isInteger(value) && value > 0 ? value : 0);

// ocr.json の 1 店を変換する。画像が変わったのに読めていない店と、長く画像を確かめていない店は配らない。
// latest_key は最後に取れた画像の sha256（画像がなければ "missing" など。ocr/plan.py が書き、ocr/merge.py が移す）。
// verified_at は、取れた画像が読んだ画像（key）と同じだと最後に確かめた時刻。読んだ時刻（checked_at）にも画像は key と
// 同じだったので、新しい方を使う
export function convertKanShop(shop, { today = todayJst(), now = Math.floor(Date.now() / 1000) } = {}) {
  if (shop.latest_key !== undefined && shop.latest_key !== shop.key) return { reason: 'image-changed' };
  let verified = Math.max(timeOf(shop.verified_at), timeOf(shop.checked_at));
  const number = /\/shop2price\/image\/(\d+)\//.exec(shop.url ?? '')?.[1];
  if (shop.latest_key === undefined && number && !LEGACY_UNCHECKED.has(number)) verified = Math.max(verified, LEGACY_VERIFIED_AT);
  if (now - verified > STALE_DAYS * DAY_SECONDS) return { reason: 'stale' };
  return convertKanOcr(shop.reads, { today });
}
