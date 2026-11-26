#!/usr/bin/env python3
"""JOYJOY公式店舗ページの最低料金を取得して保存する。

公式の料金表は「～」付きの最低価格だけなので、計算はせずにそのまま保存し、アプリは参考として表示する。
誤った「最低」を出さないため、料金表が今の形のときだけ取り込む。
- 区分（DAY / NIGHT / FREE）がちょうど 1 つずつあり、どの表も「見出し・料金行 1 行・説明 1 行」の 3 行だけ。
- 料金行の見出し、時間・曜日、説明、注記は、知っている文言（許可リスト）だけ。知らない文が 1 つでもあれば取り込まない。
- 通信の失敗（タイムアウト、接続エラー、本文の欠け、5xx、429 など）だけは前回の記録を残す。
  確認日時が進まないので、続けば publish.py が 14 日で配らなくなる。
- ページは取れたのに料金表がない・読めない・知らない文がある・開店前の店は、その日のうちに料金なしの記録
  （rates が空で、withdrawn に理由）にする。前回の最低価格は残さない。
- 年末年始・特別料金・料金改定などの告知は、読めた期間とともに notices に残す。出すかどうかは変換する日に
  scripts/web/joyjoy.js が決める（変換する日から 21 日先までと期間が重なる店や、終わりの日付が読めない店は出さない）。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests
from bs4 import BeautifulSoup, Comment, NavigableString

import crawl_plan
import net
import update_chains

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "source" / "joyjoy_prices.json"
USER_AGENT = update_chains.USER_AGENT
ALLOWED_HOST = "www.joyjoy.co.jp"
SCHEMA_VERSION = 1
DELAY = 1.5  # 1 リクエストごとにあける秒数（1 秒以上）
SAVE_EVERY = 20
JST = timezone(timedelta(hours=9))
PRICE = re.compile(r"^(?:[¥￥])?\s*(\d[\d,]*)\s*(?:円)?\s*[~～〜]$")
PERIODS = {"daytime": "DAY TIME", "nighttime": "NIGHT TIME", "freetime": "FREE TIME"}
# 料金行の見出し。「60分」「会員」などに変わったら、単位や客層が違うかもしれないので取り込まない
CHARGES = {"daytime": "お一人様 30分", "nighttime": "お一人様 30分", "freetime": "お一人様 室料"}
NOTE = "※曜日・人数・時期により、料金が変動致します。詳しくは直接店舗にお問い合わせください。"
# 最低価格としてありうる範囲（validate_data.py の JOYJOY の検査と同じ）。外れたら読み違いとみなす
SLOT_YEN = (80, 1000)
FREE_YEN = (300, 5000)
MAX_NOTICES = 20

# 料金表の中で許す文言。NFKC で正規化した後の形（「～」は「~」、全角のかっこは半角になる）
_TIME = r"(?:AM|PM)\d{1,2}:\d{2}"
# 「PM7:00~AM1:00 (金土祝前日はAM3:00迄)」のように、曜日によって終わりが延びる書き方も許す
TIME_RANGE = re.compile(rf"{_TIME}~{_TIME}(?: \([日月火水木金土祝前特・/]+は{_TIME}迄\))?")
DAY_LABEL = re.compile(r"[日月火水木金土祝平休特][日月火水木金土祝前特平休・/~]*")
PLAN = re.compile(r"(?:\(ジュース飲み放題限定\))?(?:昼|夜|深夜)?\d{1,2}時間フリー")
INCLUDED = re.compile(r"☆(?:(?:昼|夜|深夜)\d{1,2}時間フリーは)?ジュース(?:・カクテル・(?:サワー|チューハイ)・ハイボール各種)?"
                      r"飲み放題付き(?: ※土曜・特日を除く)?")

# 開店前の店（電話の「※オープン後より開通」、「2027年夏GRAND OPEN予定」など）
NOT_OPEN = re.compile(r"(?:オープン|OPEN|開店|開業)(?:後より|後から|予定|準備中)|近日(?:オープン|OPEN|開店)|COMING SOON", re.I)
# 期間の告知。料金が変わることをはっきり書いた文か、時期の語と料金の語の両方がある文
PRICE_CHANGE = re.compile(r"特別料金|特別価格|料金改定|価格改定|料金変更|料金の変更|価格変更|値上げ|値下げ|改定|改訂")
REVISION = re.compile(r"改定|改訂|値上げ|値下げ|料金変更|料金の変更|価格変更")
SEASON = re.compile(r"年末|年始|正月|大晦日|お盆|盆休|ゴールデンウィーク|(?<![A-Za-z])GW(?![A-Za-z])|繁忙期|期間限定|期間中|特別期間")
PRICE_WORD = re.compile(r"料金|価格|円|¥|フリータイム|30分|室料")
# 期間を示さない注意書きで、料金表の注記（時期により料金が変動）と同じ意味のもの。告知には数えない
KNOWN_CAVEATS = re.compile(r"※?繁忙期は料金が異なる場合がございます。?")
DATE = re.compile(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日|(?:(\d{4})/)?(\d{1,2})/(\d{1,2})")
WEEKDAY = re.compile(r"\s*\([月火水木金土日祝・]+\)")
BLOCKS = ("p", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "footer",
          "section", "table", "dt", "dd")


class Throttled(RuntimeError):
    pass


def normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value or "").split())


def lines(cell) -> list[str]:
    return [normalize(text) for text in cell.stripped_strings if normalize(text)]


def minimum_price(text: str) -> tuple[int, str]:
    display = normalize(text)
    match = PRICE.fullmatch(display)
    if not match:
        raise ValueError(f"最低価格を読み取れません: {display!r}")
    return int(match.group(1).replace(",", "")), display


def _bounded(text: str, bounds: tuple[int, int], label: str) -> tuple[int, str]:
    value, display = minimum_price(text)
    if not bounds[0] <= value <= bounds[1]:
        raise ValueError(f"{label} の最低価格が不自然です: {display!r}")
    return value, display


def _read_table(table) -> dict:
    """料金表 1 つ（DAY / NIGHT / FREE）を読む。今の形と違えば ValueError"""
    if table.find("table") is not None:
        raise ValueError("料金表の中に別の表があります")
    rows = table.find_all("tr")
    if len(rows) != 3:
        raise ValueError(f"料金表の行の数が変わりました ({len(rows)} 行)")
    title_row, price_row, note_row = rows
    title = title_row.find_all(["th", "td"], recursive=False)
    label = normalize(title[0].get_text(" ", strip=True)) if len(title) == 1 and title[0].name == "th" else ""
    period = next((key for key, text in PERIODS.items() if text == label), None)
    if period is None:
        raise ValueError(f"料金表の時間帯の見出しが変わりました: {label!r}")

    cells = price_row.find_all(["th", "td"], recursive=False)
    if [cell.name for cell in cells] != ["th", "td", "td"]:
        raise ValueError(f"{label} の料金行の形が変わりました")
    charge = " ".join(lines(cells[0]))
    if charge != CHARGES[period]:
        raise ValueError(f"{label} の料金行の見出しが変わりました: {charge!r}")
    schedules, price_texts = lines(cells[1]), lines(cells[2])
    if not schedules or not price_texts:
        raise ValueError(f"{label} の時間または金額が空です")

    if period == "freetime":
        if len(price_texts) != 1:
            raise ValueError("FREE TIME の最低価格が 1 つではありません")
        price = _bounded(price_texts[0], FREE_YEN, label)
        kinds = []
        for text in schedules:
            if TIME_RANGE.fullmatch(text):
                kinds.append("time")
            elif DAY_LABEL.fullmatch(text):
                kinds.append("day")
            elif all(PLAN.fullmatch(part) for part in text.split(" ")):
                kinds.append("plan")
            else:
                raise ValueError(f"FREE TIME の時間・曜日が知らない文です: {text!r}")
        if len(kinds) % 2 == 0 and kinds == ["day", "time"] * (len(kinds) // 2) and len(kinds) <= 8:
            windows = [{"days": schedules[index], "hours": schedules[index + 1],
                        "minimum_yen": price[0], "price_text": price[1]}
                       for index in range(0, len(schedules), 2)]
        elif kinds == ["time"] or (set(kinds) == {"plan"} and len(kinds) <= 2):
            # 時間だけの店と、時刻ではなくプラン名（夜3時間フリーなど）を書く店
            windows = [{"hours": " / ".join(schedules), "minimum_yen": price[0], "price_text": price[1]}]
        else:
            raise ValueError("FREE TIME の曜日・時間の並びが変わりました")
        unit = "per_person_per_freetime"
    else:
        for text in schedules:
            if not TIME_RANGE.fullmatch(text):
                raise ValueError(f"{label} の時間が知らない文です: {text!r}")
        if len(schedules) != len(price_texts) or len(schedules) > 3:
            raise ValueError(f"{label} の時間と最低価格の数が一致しません")
        windows = []
        for schedule, text in zip(schedules, price_texts):
            value, display = _bounded(text, SLOT_YEN, label)
            windows.append({"hours": schedule, "minimum_yen": value, "price_text": display})
        unit = "per_person_30_minutes"

    described = note_row.find_all(["th", "td"], recursive=False)
    if len(described) != 1 or described[0].name != "td" or described[0].get("colspan") != "3":
        raise ValueError(f"{label} の説明行の形が変わりました")
    included = normalize(described[0].get_text(" ", strip=True))
    if not INCLUDED.fullmatch(included):
        raise ValueError(f"{label} の説明が知らない文です: {included[:60]!r}")
    return {"period": period, "charge": charge, "unit": unit, "windows": windows, "included": [included]}


def _price_section(soup) -> tuple[list, list[dict]]:
    """「料金表」の見出しと、その直後の 表 3 つ・注記 1 つを返す。並びや文言が違えば ValueError"""
    headings = [item for item in soup.select("h5.h5_tit01")
                if normalize(item.get_text(" ", strip=True)) == "料金表"]
    if not headings:
        raise ValueError("料金表がありません")
    if len(headings) > 1:
        raise ValueError("料金表の見出しが 2 つ以上あります")
    heading = headings[0]
    elements = []
    for node in heading.next_siblings:
        if len(elements) == 4:
            break
        if isinstance(node, Comment):
            continue
        if isinstance(node, NavigableString):
            if normalize(str(node)):
                raise ValueError(f"料金表の中に知らない文があります: {normalize(str(node))[:60]!r}")
            continue
        elements.append(node)
    tables, note = elements[:3], elements[3] if len(elements) == 4 else None
    if (len(tables) != 3 or any(item.name != "table" or "price_info_list" not in item.get("class", []) for item in tables)
            or note is None or note.name != "p"):
        raise ValueError("料金表の並び（表 3 つと注記）が変わりました")
    text = normalize(note.get_text(" ", strip=True))
    if text != NOTE:
        raise ValueError(f"料金表の注記が知らない文です: {text[:60]!r}")
    rates = [_read_table(table) for table in tables]
    if sorted(rate["period"] for rate in rates) != sorted(PERIODS):
        raise ValueError("DAY・NIGHT・FREE TIME がちょうど 1 つずつではありません")
    return [heading, *elements], rates


def _page_lines(soup) -> list[str]:
    """店ごとの説明・店舗情報・お知らせの文を 1 行ずつ返す（全店共通のクーポン、スタッフ募集、エリア一覧は見ない）"""
    root = soup.select_one("article#page") or soup.body or soup
    for node in root.find_all(string=lambda text: isinstance(text, Comment)):
        node.extract()
    for node in root.find_all(["script", "style", "noscript", "iframe"]):
        node.decompose()
    for node in root.select("section#coupon, section#staff, section.bg-gray"):
        node.decompose()
    for node in root.find_all("br"):
        node.replace_with("\n")
    # お知らせ欄は閉じタグが乱れていることがあるので、ブロックの前と後ろの両方で行を分ける
    for node in root.find_all(BLOCKS):
        node.insert_before("\n")
        node.append("\n")
    return [line for line in (normalize(text) for text in root.get_text("").split("\n")) if line]


def _resolve_dates(parts: list[tuple[int | None, int, int]], seen_on: date) -> list[date] | None:
    """年のない日付に年を付ける。12/28~1/4 のように年をまたぐときは後ろの日付を翌年にする。
    年は、終わりが見た日の 31 日前より後になる最初の年（去年・今年・来年の順に試す）"""
    years = [parts[0][0]] if parts[0][0] is not None else [seen_on.year - 1, seen_on.year, seen_on.year + 1]
    found = None
    for year in years:
        try:
            current, dates = year, []
            for explicit, month, day in parts:
                if explicit is not None:
                    current = explicit
                value = date(current, month, day)
                if dates and value < dates[-1] and explicit is None:
                    current += 1
                    value = date(current, month, day)
                dates.append(value)
        except ValueError:
            continue
        found = dates
        if dates[-1] >= seen_on - timedelta(days=31):
            break
    return found


def notice_period(text: str, seen_on: date) -> tuple[str | None, str | None]:
    """告知の文から期間（ISO の日付。読めない側は None）を読む。終わりが None なら変換で出さない"""
    parts, matches = [], list(DATE.finditer(text))
    for match in matches:
        if match.group(2):
            year, month, day = match.group(1), match.group(2), match.group(3)
        else:
            year, month, day = match.group(4), match.group(5), match.group(6)
        parts.append((int(year) if year else None, int(month), int(day)))
    if not parts:
        return None, None
    dates = _resolve_dates(parts, seen_on)
    if dates is None:
        return None, None
    first, last = min(dates), max(dates)
    if len(dates) == 1:
        before = text[:matches[0].start()].rstrip()
        after = WEEKDAY.sub("", text[matches[0].end():], count=1).lstrip()
        if after.startswith(("まで", "迄")) or before.endswith(("~", "〜", "-")):
            return None, last.isoformat()
        # 「12/1より」「12/1 料金改定」は、その日からずっと（終わりの日付がない）
        if after.startswith(("~", "〜", "-", "より", "から", "以降")) or REVISION.search(text):
            return first.isoformat(), None
    return first.isoformat(), last.isoformat()


def parse_page(url: str, markup: str, now: int | None = None) -> dict:
    """表の金額はすべて「～」付きの最低価格として保持する。取り込めないときは ValueError（理由）"""
    now = int(time.time()) if now is None else now
    soup = BeautifulSoup(markup, "html.parser")
    section, rates = _price_section(soup)
    for node in section:
        node.extract()
    page_lines = _page_lines(soup)
    for text in page_lines:
        if NOT_OPEN.search(text):
            raise ValueError(f"開店前の店です: {text[:60]!r}")
    seen_on = datetime.fromtimestamp(now, JST).date()
    notices = []
    for text in page_lines:
        if KNOWN_CAVEATS.fullmatch(text):
            continue
        if PRICE_CHANGE.search(text) or (SEASON.search(text) and PRICE_WORD.search(text)):
            start, end = notice_period(text, seen_on)
            notices.append({"text": text[:200], "from": start, "until": end})
    if len(notices) > MAX_NOTICES:
        raise ValueError("期間の告知が多すぎます")
    record = {"page": url, "rates": rates, "note": NOTE}
    if notices:
        record["notices"] = notices
    record["checked_at"] = now
    return record


def withdrawn(url: str, reason: str, now: int) -> dict:
    """ページは取れたが料金を出せない店の記録。前回の最低価格は残さない"""
    return {"page": url, "rates": [], "withdrawn": reason[:200], "checked_at": now}


def fetch_shop(fetcher: net.Fetcher, url: str, now: int) -> dict:
    """1 店を取って記録を返す。通信の失敗は例外（requests.RequestException、Throttled、net.OutOfTime）"""
    response = fetcher.get(url, html=True)
    status = response.status_code
    if status in (429, 503):
        raise Throttled(f"HTTP {status}")
    if status in (404, 410):
        return withdrawn(url, f"店舗ページがありません (HTTP {status})", now)
    if status != 200:
        raise requests.HTTPError(f"HTTP {status}")
    final = urlsplit(response.url or url)
    if final.hostname != ALLOWED_HOST:
        raise requests.HTTPError(f"公式サイト以外へ転送されました ({final.hostname})")
    if unquote(final.path).rstrip("/").lower() != unquote(urlsplit(url).path).rstrip("/").lower():
        return withdrawn(url, f"店舗ページが別のページへ転送されました ({final.path[:80]})", now)
    if not response.encoding or response.encoding.lower() == "iso-8859-1":
        response.encoding = "utf-8"
    try:
        return parse_page(url, response.text, now)
    except ValueError as error:
        return withdrawn(url, str(error), now)
    except Exception as error:  # 読み取りの不具合でも、前回の値を残さず料金なしにする
        return withdrawn(url, f"料金表を読めません ({type(error).__name__})", now)


def load_document() -> dict:
    if not DATA_FILE.exists():
        return {}
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def load_previous() -> dict[str, dict]:
    return load_document().get("shops", {})


def save(shops: dict[str, dict], updated_at: int) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": SCHEMA_VERSION, "updated_at": updated_at, "shops": shops}
    temporary = DATA_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
                         encoding="utf-8", newline="")
    os.replace(temporary, DATA_FILE)


def rate_count(shops: dict[str, dict]) -> int:
    return sum(bool(shop.get("rates")) for shop in shops.values())


def update(log=print, *, fetcher: net.Fetcher | None = None, pause=time.sleep) -> bool:
    chains = update_chains.load_previous()
    joyjoy = {key: item for key, item in chains.items() if item.get("chain") == "JOYJOY"}
    if not joyjoy:
        log("chains.json にJOYJOY店舗がないので、前回の値を残します")
        return False

    document = load_document()
    previous = {key: value for key, value in document.get("shops", {}).items() if key in joyjoy}
    previous_updated_at = document.get("updated_at", 0)
    now = int(time.time())
    shops = dict(previous)
    # 店ごとに 3 日に 1 回、古い順に取る（crawl_plan.py）。URL が変わった店は毎回取る
    due = crawl_plan.due(list(joyjoy), shops.get, "source/joyjoy_prices.json", now,
                         force=lambda key: shops.get(key, {}).get("page") != joyjoy[key].get("url"))
    fetcher = fetcher or net.Fetcher(USER_AGENT, label="JOYJOY")

    def keep() -> dict:
        return {key: shops[key] for key in joyjoy if key in shops}

    failed = fetched = dropped = 0
    stopped = ""
    for index, key in enumerate(due):
        url = joyjoy[key].get("url", "")
        if not url.startswith(f"https://{ALLOWED_HOST}/store/view/"):
            log(f"  取得失敗 {key} (店舗URLがJOYJOY公式ドメインではありません)")
            failed += 1
            continue
        if index:
            pause(DELAY)
        try:
            record = fetch_shop(fetcher, url, int(time.time()))
        except net.OutOfTime:
            stopped = f"締め切りを過ぎたので残り {len(due) - index} 店は取りません"
            break
        except Throttled as error:
            failed += 1
            stopped = f"公式サイトの制限 ({error}) で残り {len(due) - index - 1} 店は取りません"
            break
        except requests.RequestException as error:
            log(f"  取得失敗 {key} ({type(error).__name__}: {str(error)[:120]})")
            failed += 1
            continue
        shops[key] = record
        fetched += 1
        if not record["rates"]:
            dropped += 1
            log(f"  料金なし {key} ({record['withdrawn']})")
        if fetched % SAVE_EVERY == 0:
            save(keep(), previous_updated_at)

    if stopped:
        log(f"  {stopped}")
    shops = keep()
    updated_at = now if not failed and not stopped else previous_updated_at
    save(shops, updated_at)
    log(f"JOYJOY: 最低料金 {rate_count(shops)}/{len(joyjoy)} 店 (今回取得 {fetched}、料金なし {dropped}、"
        f"失敗 {failed}、通信 {fetcher.bytes // 1024} KB)")
    return not failed


def main() -> int:
    return 0 if update(log=lambda line: print(line, flush=True)) else 1


if __name__ == "__main__":
    sys.exit(main())
