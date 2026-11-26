#!/usr/bin/env python3
"""公式 JOYSOUND 店舗ページから標準料金表を取得する。

店ごとの記録は status で状態を持つ。金額に変換するのは status が priced の店だけ。
- priced: 料金表を読めて、まだ終わっていない期間告知がない
- contact: 「料金は店舗にお問い合わせください」だけで、料金表がない
- season: 年末年始・特別料金などの告知が、まだ終わっていない（表と実際の料金が違い得る）。
  アプリは先の日付をいくらでも選べるので、期間がどれだけ先でも出さない
- unparsed: ページは取れたが、料金表を読めない（表の形が変わったなど）
- unverified: 旧形式の記録で、取り直せなかった（期間告知や問い合わせを確かめていない）
通信の失敗（タイムアウト、接続エラー、5xx、429、公式の店舗ページではない応答）のときだけ前回の記録を残す。
seen.json の日時が進まないので、14 日確かめられなければ publish.py が配らなくなる。
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
from urllib.parse import urlsplit

import requests

import crawl_plan
import net
from bs4 import BeautifulSoup

import update

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "source" / "joysound_prices.json"
USER_AGENT = update.USER_AGENT
SCHEMA_VERSION = 1
DELAY = 1.5  # 1 リクエストごとに 1 秒以上あける。1 店ずつ順に取る
THROTTLED_CODES = (429, 503)
STOP_AFTER = 5  # 続けてこの数の店で通信に失敗したら、残りの店は今回は取らない
SCOPE = "standard-rates-v2"  # v2: status と期間告知（notices）を持つ
STATUSES = ("priced", "contact", "season", "unparsed", "unverified")
JST = timezone(timedelta(hours=9))
PRICE_PATTERN = re.compile(r"([\d,]+)\s*円")
NO_PRICE = {"", "-", "−", "ー", "―", "—"}
CONTACT_TEXT = "料金は店舗にお問い合わせください"
# 料金欄の文章にこれらの語があれば、期間で料金が変わる告知とみなす（.jt-season 以外の書き方の告知）
SEASON_WORDS = ("特別料金", "年末年始", "期間限定", "期間中", "特別期間", "料金改定", "価格改定", "改定",
                "お盆", "GW", "ゴールデンウィーク", "繁忙期", "特定日", "大晦日", "元旦", "元日", "正月",
                "クリスマス", "臨時料金")
_DATE = r"(?:(\d{4})[年/])?(\d{1,2})[月/](\d{1,2})日?(?:\([^)]{1,3}\))?"
_END = r"(?:(\d{4})[年/])?(?:(\d{1,2})[月/])?(\d{1,2})日?(?:\([^)]{1,3}\))?"
DATE_RANGE = re.compile(_DATE + r"(?:[〜～~\-－]|から)" + _END)
# 終わりの日付のない「12/27〜」も始まりだけの告知として読む（その 1 日だけの告知にしない）
ONE_DATE = re.compile(_DATE + r"(まで|迄|から|より|以降|[〜～~\-－])?")


class Throttled(RuntimeError):
    pass


class FetchFailed(RuntimeError):
    """通信の失敗。前回の記録を残す。"""


def clean(text: str | None) -> str:
    return " ".join(unicodedata.normalize("NFKC", text or "").split())


def compact(text: str | None) -> str:
    return clean(text).replace(" ", "")


def class_for_header(text: str) -> str | None:
    text = clean(text)
    if "学生" in text or "学割" in text:
        return "学生"
    if "シニア" in text:
        return "シニア"
    if "非会員" in text or "一般" in text:
        return "一般"
    if "会員" in text:
        return "会員"
    return None


def tax_included_price(cell) -> int | None:
    """Return the displayed tax-included amount, never the pre-tax amount."""
    text = clean(cell.get_text(" ", strip=True))
    compact_text = text.replace(" ", "")
    if compact_text in NO_PRICE:
        return None

    tax_cell = cell.select_one(".pt")
    if tax_cell is not None:
        text = clean(tax_cell.get_text(" ", strip=True))
        compact_text = text.replace(" ", "")
    if any(mark in compact_text for mark in ("税別", "税抜")):
        raise ValueError(f"税抜の金額しかありません: {text!r}")

    # The current site renders the tax-included amount in .pt. Older pages may
    # have the same value as the only amount in the cell, so accept one amount.
    matches = PRICE_PATTERN.findall(compact_text)
    if len(matches) == 1:
        return int(matches[0].replace(",", ""))
    if "税込" in compact_text:
        after_tax = compact_text.split("税込", 1)[1]
        match = PRICE_PATTERN.search(after_tax)
        if match:
            return int(match[1].replace(",", ""))
    raise ValueError(f"税込の金額を 1 つに決められません: {text!r}")


def drink_tag(cell) -> str:
    text = clean(cell.get_text(" ", strip=True)).replace(" ", "")
    if "ドリンクバー" in text and any(mark in text for mark in ("付き", "付", "込")):
        return "ドリンクバー付"
    if "1ドリンク" in text or "ワンドリンク" in text or "1オーダー" in text:
        return "1ドリンク別"
    return ""


def official_simple_hours(soup: BeautifulSoup) -> str:
    """公式ページの営業時間が曜日例外なしの単純な範囲なら返す。
    LASTに推測時刻を充てないため、注意書き・曜日差分があれば空にする。
    """
    lines = [clean(text) for text in soup.stripped_strings]
    candidates = []
    for index, value in enumerate(lines[:-1]):
        if value != "営業時間":
            continue
        candidate = lines[index + 1].replace(" ", "")
        # 24時間営業、曜日別閉店時刻、臨時の例外は安全に断定できない。
        if not re.fullmatch(r"\d{1,2}:\d{2}[～〜~-]翌?\d{1,2}:\d{2}", candidate):
            continue
        if index + 2 < len(lines) and lines[index + 2].startswith(("※", "＊", "*")):
            continue
        candidates.append(candidate)
    return candidates[0].replace("~", "～") if len(set(candidates)) == 1 else ""


# ---- 期間告知 ----

def day_start(now: int) -> int:
    """今日（JST）の 0 時。"""
    today = datetime.fromtimestamp(now, JST).date()
    return int(datetime(today.year, today.month, today.day, tzinfo=JST).timestamp())


def overlaps(notice: dict, now: int) -> bool:
    """告知がまだ終わっていないか。終わりが読めない告知は続いているとみなす。
    アプリは先の日付をいくらでも選べるので、始まりがどれだけ先でも、終わるまでは表の金額を出さない。"""
    until = notice.get("until")
    return until is None or until > day_start(now)


def _epoch(day: date, *, end: bool = False) -> int:
    # 終わりの日は深夜の営業（翌朝 6 時まで）まで含める
    moment = datetime(day.year, day.month, day.day, tzinfo=JST) + (timedelta(days=1, hours=6) if end else timedelta())
    return int(moment.timestamp())


def _resolve(today: date, year, month, day, end_month, end_day, end_year) -> tuple[date, date] | None:
    """期間を年に置く。年のない期間は、去年・今年・来年のうち今日に最も近い回にする（同じ近さなら先の回）。
    12/27〜1/5 のように年をまたぐ期間も扱う。

    「12月27日より」のように始まりだけの日付は、12/28 に来年の 12/27 と読むと、始まった翌日に告知が消えて
    表の金額が出てしまう。近い回に置けば、始まった後は「もう始まっている」と読める。
    「1月5日まで」のように終わりだけの日付も、過ぎた後は終わった告知と読める（来年の 1/5 まで続くとしない）。
    半年より前に告知が載っていても、まだ先の回とは読めない（過ぎた回として扱う）。"""
    month, day = int(month), int(day)
    end_month = int(end_month) if end_month else month
    end_day = int(end_day)
    best = None
    for first in [int(year)] if year else [today.year - 1, today.year, today.year + 1]:
        try:
            begins = date(first, month, day)
            last_year = int(end_year) if end_year else first + (1 if (end_month, end_day) < (month, day) else 0)
            until = date(last_year, end_month, end_day)
        except ValueError:  # 2/30 のような日付。うるう年でない年の 2/29 はほかの年だけを比べる
            continue
        if until < begins:
            return None
        gap = (begins - today).days if begins > today else (today - until).days if until < today else 0
        rank = (gap, begins <= today)  # 同じ近さなら先の回（出さない側）
        if best is None or rank < best[0]:
            best = (rank, begins, until)
    return (best[1], best[2]) if best else None


def text_notices(text: str, today: date) -> list[dict]:
    """期間告知の語がある文から期間を読む。日付が読めなければ期間不明（無期限）にする。"""
    notices = []
    for sentence in re.split(r"[※。\n]", text):
        sentence = sentence.replace(" ", "")
        if not any(word in sentence for word in SEASON_WORDS):
            continue
        found = []
        rest = sentence
        for match in DATE_RANGE.finditer(sentence):
            span = _resolve(today, match[1], match[2], match[3], match[5], match[6], match[4])
            found.append((_epoch(span[0]), _epoch(span[1], end=True)) if span else (None, None))
            rest = rest.replace(match[0], "")
        for match in ONE_DATE.finditer(rest):
            span = _resolve(today, match[1], match[2], match[3], None, match[3], None)
            if span is None:
                found.append((None, None))
            elif match[4] in ("まで", "迄"):
                found.append((None, _epoch(span[1], end=True)))
            elif match[4]:  # より・から・以降・「12/27〜」: 終わりが分からない
                found.append((_epoch(span[0]), None))
            else:
                found.append((_epoch(span[0]), _epoch(span[1], end=True)))
        for begins, until in found or [(None, None)]:
            notices.append({"from": begins, "until": until, "text": sentence[:80]})
    return notices


def _iso_epoch(value: str | None) -> int | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=JST)
    return int(moment.timestamp())


def season_notices(soup: BeautifulSoup, blocks: list, now: int) -> list[dict]:
    """.jt-season の掲示期間と、料金欄の文章にある期間告知。終わった告知は除く。"""
    notices = []
    for notice in soup.select(".jt-season"):
        until = _iso_epoch(notice.get("data-until"))
        begins = _iso_epoch(notice.get("data-from"))
        notices.append({"from": begins, "until": until, "text": compact(notice.get_text(" ", strip=True))[:80]})
    today = datetime.fromtimestamp(now, JST).date()
    for _, contents, _ in blocks:
        notices.extend(text_notices(_block_text(contents), today))
    return [notice for notice in notices if overlaps(notice, now)]


def _block_text(contents) -> str:
    """料金欄の文章（表の見出し・プラン名・注記）。.jt-season は日時で判定するので除く。"""
    copy = BeautifulSoup(str(contents), "html.parser")
    for tag in copy.select("style, script, .jt-season, .jt-contact"):
        tag.decompose()
    return "\n".join(clean(text) for text in copy.stripped_strings)


DRINK_HINT = re.compile(r"ドリンク|オーダー|飲み物|注文")


def _drink_lines(element) -> list[str]:
    """表の外の文章のうち、飲み物の決まりに関わる行（「※全プランワンドリンクオーダー制」など）。変換で読む。"""
    copy = BeautifulSoup(str(element), "html.parser")
    for tag in copy.select("style, script, .jt, .jt-season, .jt-contact"):
        tag.decompose()
    return [line for line in (clean(text) for text in copy.stripped_strings) if DRINK_HINT.search(line)]


def _remarks(soup: BeautifulSoup) -> list[str]:
    """「備考」の欄の飲み物の決まり。昼・夜の両方に当てはめる。"""
    lines = []
    for heading in soup.find_all("h2"):
        if clean(heading.get_text(" ", strip=True)) == "備考":
            body = heading.find_next_sibling()
            if body is not None:
                lines.extend(_drink_lines(body))
    return lines


# ---- ページの読み取り ----

def _same_page(url: str | None, page: str) -> bool:
    if not url:
        return False
    a, b = urlsplit(url.strip()), urlsplit(page)
    return a.hostname == b.hostname and a.path.rstrip("/") == b.path.rstrip("/")


def parse_page(html: str, page: str, now: int | None = None) -> dict:
    """店のページを読み、状態付きの記録を返す。読めないページは ValueError。

    canonical も og:url もこの店のページを指していない応答は、料金の欄があれば（ほかの店のページかもしれない）
    ValueError、料金の欄もなければ途中の経路のブロック画面などとみなして FetchFailed（通信の失敗と同じ扱い）。
    """
    now = int(time.time()) if now is None else now
    soup = BeautifulSoup(html, "html.parser")
    headings = [heading for heading in soup.find_all("h2")
                if clean(heading.get_text(" ", strip=True)) in ("昼の料金", "夜の料金")]
    canonical = soup.select_one('link[rel="canonical"]')
    og_url = soup.select_one('meta[property="og:url"]')
    if not (_same_page(canonical.get("href") if canonical else None, page)
            or _same_page(og_url.get("content") if og_url else None, page)):
        if headings:
            raise ValueError("この店のページか確かめられません（canonical・og:url が違います）")
        raise FetchFailed("公式の店舗ページではない応答です")
    base = {"page": page, "scope": SCOPE, "checked_at": now}

    blocks = []
    for heading in headings:
        title = clean(heading.get_text(" ", strip=True))
        contents = heading.find_next_sibling("div", class_="charges-special__contents")
        if contents is None:
            raise ValueError(f"料金の欄に中身がありません: {title}")
        blocks.append((title, contents, "昼" if "昼" in title else "夜"))
    if not blocks:
        raise ValueError("料金の欄（昼の料金・夜の料金）がありません")

    # 料金表を出さず「料金は店舗にお問い合わせください」だけの店。片方の欄だけ表があっても、一部の料金は出さない
    contact = [contents for _, contents, _ in blocks if contents.select(".jt-contact")]
    if contact:
        if not all(CONTACT_TEXT in compact(c.get_text(" ", strip=True)) for c in contact):
            raise ValueError("問い合わせの欄の文言が想定と違います")
        return {**base, "status": "contact", "sections": []}

    # 年末年始などの告知がまだ終わっていなければ、表と実際の料金が違い得るので出さない。
    # アプリは先の日付をいくらでも選べるので、期間がどれだけ先でも出さない
    notices = season_notices(soup, blocks, now)
    if notices:
        untils = [notice["until"] for notice in notices]
        return {**base, "status": "season", "sections": [], "notices": notices,
                "season_until": None if None in untils else max(untils)}

    sections = []
    notes = {}
    remarks = _remarks(soup)
    for title, contents, period in blocks:
        tables = contents.select(".jt")
        if not tables:
            raise ValueError(f"料金表も問い合わせの案内もありません: {title}")
        notes[period] = notes.get(period, remarks[:]) + _drink_lines(contents)
        for table in tables:
            header = table.select_one(".jr-h")
            if header is None:
                raise ValueError(f"料金表に列の見出しがありません: {title}")
            cells = header.find_all(class_="jc", recursive=False)
            if len(cells) < 3 or "jc-k" not in cells[0].get("class", []):
                raise ValueError(f"料金表の見出しの形が想定と違います: {title}")
            day = clean(cells[0].get_text(" ", strip=True))
            customers = [class_for_header(cell.get_text(" ", strip=True)) for cell in cells[1:]]
            if not day or any(value is None for value in customers) or len(set(customers)) != len(customers):
                raise ValueError(f"料金区分の列を読めません: {title} / {day} / {customers}")

            rows = []
            for row in table.select(".jr:not(.jr-h)"):
                row_cells = row.find_all(class_="jc", recursive=False)
                if len(row_cells) != len(customers) + 1 or "jc-k" not in row_cells[0].get("class", []):
                    raise ValueError(f"料金表の行の形が想定と違います: {title} / {day}")
                label = clean(row_cells[0].get_text(" ", strip=True))
                prices = {customer: tax_included_price(cell) for customer, cell in zip(customers, row_cells[1:])}
                drinks = {customer: drink_tag(cell) for customer, cell in zip(customers, row_cells[1:])}
                if any(value is not None and not 1 <= value <= 30000 for value in prices.values()):
                    raise ValueError(f"不自然な金額があります: {title} / {day}: {label}")
                if any(value is not None for value in prices.values()):
                    rows.append({"label": label, "prices": prices, "drinks": drinks})
            if not rows:
                raise ValueError(f"料金表に金額がありません: {title} / {day}")
            sections.append({"period": period, "day": day, "rows": rows})

    if not any("30分" in row["label"] for section in sections for row in section["rows"]):
        raise ValueError("通常の 30 分料金の表がありません")
    return {**base, "status": "priced", "sections": sections, "notes": notes, "notices": notices,
            "opening_hours": official_simple_hours(soup)}


# ---- 取得 ----

def load_json(path: Path, default: dict) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save(shops: dict[str, dict], updated_at: int) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"schema_version": SCHEMA_VERSION, "updated_at": updated_at, "shops": shops},
                         ensure_ascii=False, separators=(",", ":"))
    temp = DATA_FILE.with_suffix(".json.tmp")
    temp.write_text(payload + "\n", encoding="utf-8", newline="")
    os.replace(temp, DATA_FILE)


def official_page(page: str) -> bool:
    parts = urlsplit(page)
    return parts.scheme == "https" and parts.hostname == "shop.joysound.com" and parts.path.startswith("/shop/")


def fetch_html(fetcher: net.Fetcher, page: str) -> str:
    """1 店のページを 1 回取る。通信の失敗は FetchFailed / Throttled / requests の例外。
    ページがない（404・別のページへの転送）ときは ValueError（料金なしにする）。"""
    if not official_page(page):
        raise ValueError(f"公式の JOYSOUND 店舗ページの URL ではありません: {page}")
    response = fetcher.get(page, html=True)  # 通信の失敗は net.Fetcher が新しい接続で読み直す
    if response.status_code in THROTTLED_CODES:
        raise Throttled(f"HTTP {response.status_code}")
    if response.status_code >= 500:
        raise FetchFailed(f"HTTP {response.status_code}")
    if response.status_code in (404, 410):
        raise ValueError(f"店のページがありません (HTTP {response.status_code})")
    if response.status_code != 200:
        raise FetchFailed(f"HTTP {response.status_code}")
    final = getattr(response, "url", None) or page
    if not _same_page(final, page):
        raise ValueError(f"店のページが別のページへ転送されました: {final}")
    response.encoding = "utf-8"
    return response.text


def no_price(page: str, status: str, checked_at: int, reason: str = "") -> dict:
    record = {"page": page, "scope": SCOPE, "status": status, "sections": [], "checked_at": checked_at}
    if reason:
        record["reason"] = reason[:200]
    return record


def needs_refresh(record: dict | None, page: str, now: int) -> bool:
    """3 日を待たずに取り直す店。旧形式・URL の変更・期間告知の終わり・旧形式から移せなかった店。"""
    record = record or {}
    if record.get("scope") != SCOPE or record.get("page") != page or record.get("status") == "unverified":
        return True
    until = record.get("season_until")
    return record.get("status") == "season" and isinstance(until, int) and now >= until


def update(log=print, fetcher: net.Fetcher | None = None, now: int | None = None) -> bool:
    chain_data = load_json(ROOT / "chains.json", {"shops": []})
    listed = {shop["id"]: shop["url"] for shop in chain_data["shops"]
              if shop.get("chain") == "JOYSOUND"}
    if not listed:
        raise ValueError("JOYSOUND shops are missing from chains.json")

    old_doc = load_json(DATA_FILE, {"updated_at": 0, "shops": {}})
    previous = old_doc.get("shops", {})
    now = int(time.time()) if now is None else now
    selected = {key: previous[key] for key in listed if key in previous}
    # 店ごとに 3 日に 1 回、古い順に取る（crawl_plan.py）。旧形式・URL の変更・期間告知の終わった店は毎回取る
    due = crawl_plan.due(list(listed), selected.get, "source/joysound_prices.json", now,
                         force=lambda key: needs_refresh(selected.get(key), listed[key], now))
    fetcher = fetcher or net.Fetcher(USER_AGENT, label="JOYSOUND")
    failures, streak = [], 0
    try:
        for index, key in enumerate(due):
            if index:
                time.sleep(DELAY)
            try:
                html = fetch_html(fetcher, listed[key])
                selected[key] = parse_page(html, listed[key], now)
                streak = 0
            except Throttled as exc:
                failures.append(key)
                log(f"JOYSOUND {key}: 公式サイトに断られたので、残りの店は今回は取りません ({exc})")
                break
            except (requests.RequestException, FetchFailed) as exc:
                failures.append(key)
                streak += 1
                log(f"JOYSOUND {key}: 取得に失敗したので前回の記録を残します ({type(exc).__name__}: {exc})")
                if streak >= STOP_AFTER:
                    log(f"JOYSOUND: {STOP_AFTER} 店続けて取得に失敗したので、残りの店は今回は取りません")
                    break
            except net.OutOfTime:
                raise
            except Exception as exc:  # noqa: BLE001 ページは取れたのに読めない（想定外の形で読み取りが落ちたときも）
                # 前回の金額を残さず、料金なしにする。読み直しても同じ HTML なので、もう一度は取らない
                selected[key] = no_price(listed[key], "unparsed", now, f"{type(exc).__name__}: {exc}")
                streak = 0
                log(f"JOYSOUND {key}: 料金表を読めないので料金なしにします ({type(exc).__name__}: {exc})")
    except net.OutOfTime:
        log("JOYSOUND: 締め切りを過ぎたので、残りの店は取らずに取れた分を保存します")

    # 旧形式の記録（期間告知や問い合わせを確かめていない表）は、取り直せなくても料金として残さない
    for key, record in list(selected.items()):
        if record.get("scope") != SCOPE:
            selected[key] = no_price(listed[key], "unverified", record.get("checked_at", 0)
                                     if isinstance(record.get("checked_at"), int) else 0)
    selected = {key: selected[key] for key in listed if key in selected}
    save(selected, old_doc.get("updated_at", 0) if failures else now)
    states = {state: sum(shop.get("status") == state for shop in selected.values()) for state in STATUSES}
    log(f"JOYSOUND: {len(selected)}/{len(listed)} 店 (料金表 {states['priced']}, 店舗問い合わせ {states['contact']}, "
        f"期間告知中 {states['season']}, 読めない {states['unparsed']}, 未確認 {states['unverified']}, "
        f"今回の取得 {len(due)} 店のうち通信の失敗 {len(failures)})")
    return not failures


if __name__ == "__main__":
    try:
        success = update(log=lambda line: print(line, flush=True))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"JOYSOUND price update failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(0 if success else 1)
