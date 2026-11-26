#!/usr/bin/env python3
"""公式のメガビッグ店舗ページから店舗情報と料金表を取得する。金額への変換は web/megabig.js が行う。

- 店の一覧（/shop/）は毎回 1 回読む。店のページは crawl_plan.py で 3 日に 1 回ずつ、古い順に読む。
  リクエストの間は 1 秒以上あける。閉店の日付を過ぎた店（店名の【YYYY/M/D閉店】）は読まない。
- 通信の失敗（タイムアウト、接続エラー、本文の欠け、5xx、429）では前回の記録を残す。確認日時は進まないので、
  続けば 14 日で publish.py が配らなくなる。429・403・503 のときは残りの店も取らない。
- ページは読めたのに料金表がない・読めない（404 も含む）ときは、その日のうちに料金なしの記録（missing）にする。
  前回の金額は残さない。記録は残すので、店数の減少として検査に止められない。
- 一覧から消えた店と閉店した店は料金の記録を消す（店の一覧 chains.json の行は残す。理由は MAX_REMOVED を参照）。
- 料金のセルは数字（とカンマと円）だけを読む。「-」はその曜日は提供なしとして残す。読めないセルのある表は捨て、
  備考と一緒に skipped に残す（変換はその店を出さず、警告に出す）。
- #price の HTML コメントは読む前に取り除く（去年の告知がコメントで残っている）。表の外の文（notices）と
  #price のリンク（links）も残し、変換で期間料金の告知を判定する。
- CRAWL_DEADLINE を過ぎたら残りの店は取らずに、そこまでの分を保存して終わる。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup, Comment

sys.path.insert(0, str(Path(__file__).resolve().parent))
import crawl_plan  # noqa: E402
import net  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CHAIN_FILE = ROOT / "chains.json"
PRICE_FILE = ROOT / "source" / "megabig_prices.json"
SOURCE = "source/megabig_prices.json"
INDEX_URL = "https://megakara.jp/shop/"
CHAIN = "メガビッグ"
USER_AGENT = "karaoke-auto-getter/1.0 (public price data; GitHub Actions)"
DELAY = 1.0  # リクエストの間隔（秒）
PREFIX = "mb-"
# 公式サイトに断られたとみなして残りを取らない状態
STOP_CODES = (403, 429, 503)
# ページがない。料金なしの記録にする
GONE_CODES = (404, 410)
# 1 回に消す料金の記録の数。validate_data.py は小さいチェーンでも 3 店以上の減少を異常として、
# チェーンの元データ全体を前回に戻す。それより多く一覧から消えたときは、一覧の読み違いとみなして消さない
MAX_REMOVED = 2
PREFECTURE = re.compile(r"^(北海道|東京都|大阪府|京都府|.{2,3}県)")
SHOP_PATH = re.compile(r"^/shop/(\d{3,6})/?$")
MAP_COORDINATES = re.compile(r"!2d(-?\d+(?:\.\d+)?)!3d(-?\d+(?:\.\d+)?)")
CLOSED = re.compile(r"【(\d{4})/(\d{1,2})/(\d{1,2})閉店】")
PRICE_CELL = re.compile(r"[¥]?(\d{1,3}(?:,\d{3})+|\d+)円?")
DASHES = {"-", "‐", "‑", "‒", "–", "—", "―", "−", "ー"}
# 表の外の文として読む要素（入れ子は外側だけ）
JST = timezone(timedelta(hours=9))
BLOCKS = ("h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "dt", "dd", "caption", "figcaption", "blockquote")


class Stopped(Exception):
    """公式サイトに断られた（429 など）。残りの店は取らない。"""


class Failed(Exception):
    """通信の失敗（5xx など）。前回の記録を残す。"""


class Gone(Exception):
    """ページがない（404・410）。料金なしの記録にする。"""


def clean(value: str | None) -> str:
    return " ".join((value or "").replace("\xa0", " ").split())


def fetch(fetcher: net.Fetcher, url: str) -> str:
    """HTML を読む。通信の失敗は requests の例外か Failed、締め切りは net.OutOfTime を投げる。"""
    response = fetcher.get(url, html=True)
    if response.status_code in STOP_CODES:
        raise Stopped(f"HTTP {response.status_code}: {url}")
    if response.status_code in GONE_CODES:
        raise Gone(f"HTTP {response.status_code}: {url}")
    if response.status_code != 200:
        raise Failed(f"HTTP {response.status_code}: {url}")
    # 閉店した店のページが一覧などへ転送されると、別の店の情報を読んでしまう
    final = getattr(response, "url", None) or url
    if urlsplit(final).path.rstrip("/") != urlsplit(url).path.rstrip("/"):
        raise Gone(f"別のページへ転送されました: {final}")
    response.encoding = "utf-8"
    return response.text


def field_text(soup: BeautifulSoup, label: str) -> str:
    for term in soup.select("dt"):
        if clean(term.get_text(" ", strip=True)).startswith(label):
            definition = term.find_next_sibling("dd")
            return clean(definition.get_text(" ", strip=True)) if definition else ""
    return ""


def cell_text(cell) -> str | None:
    """金額のセルを数字だけの文字列にする。「-」は "-"。それ以外（「980円※2」など）は None。"""
    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", cell.get_text("", strip=True)))
    if text in DASHES:
        return "-"
    match = PRICE_CELL.fullmatch(text)
    return match.group(1).replace(",", "") if match else None


def parse_sections(root) -> tuple[list[dict], list[dict]]:
    """#price の .tbl-wrap を表にする。読めない表は捨てて (時間帯, 見出し, 備考, 理由) を返す。
    捨てた表の備考も残す（期間料金や 1 人の料金の文が書いてあれば、変換で店ごと判定に使う）。"""
    sections, skipped = [], []
    for wrapper in root.select(".tbl-wrap"):
        table = wrapper.select_one("table.tbl")
        titlebar = wrapper.select_one(".tbl-ttl")
        head = table.select_one("tr.fttitle") if table else None
        cells = head.select("th,td") if head else []
        time_text = clean(titlebar.get_text(" ", strip=True)) if titlebar else ""
        title = clean(cells[0].get_text("", strip=True)) if cells else ""
        note = clean(" ".join(p.get_text(" ", strip=True) for p in table.select("tr.txtbox p"))) if table else ""

        def skip(reason: str) -> None:
            skipped.append({"time": time_text, "title": title, "note": note, "reason": reason})

        if not table or not titlebar or len(cells) < 2:
            skip("shape")
            continue
        header = [title]
        header.extend(clean(cell.get_text(" ", strip=True)) for cell in cells[1:])
        rows, reason = [], ""
        for row in table.select("tr.pricebox"):
            name = row.find("th")
            values = [cell_text(cell) for cell in row.select("td")]
            if not name or len(values) != len(header) - 1:
                reason = "shape"
                break
            if any(value is None for value in values):
                reason = "price-cell"
                break
            rows.append([clean(name.get_text(" ", strip=True)), *values])
        if reason or not rows:
            skip(reason or "no-rows")
            continue
        heading = next((clean(h.get_text(" ", strip=True)) for h in wrapper.find_all_previous("h3")
                        if clean(h.get_text(" ", strip=True))), "")
        sections.append({
            "heading": heading,
            "time": time_text,
            "title": title,
            "note": note,
            "header": header,
            "rows": rows,
        })
    return sections, skipped


def notices_of(root) -> list[str]:
    """表の外の文（見出し、お知らせ）を要素ごとに返す。期間料金の告知は変換で判定する。"""
    copy = BeautifulSoup(str(root), "html.parser")
    for wrapper in copy.select(".tbl-wrap"):
        wrapper.decompose()
    texts = []
    outer = [element for element in copy.find_all(BLOCKS) if not element.find_parent(BLOCKS)]
    for element in outer:
        text = clean(element.get_text(" ", strip=True))
        if text and text not in texts:
            texts.append(text)
        element.decompose()
    rest = clean(copy.get_text(" ", strip=True))
    if rest and rest not in texts:
        texts.append(rest)
    return texts


def price_record(soup: BeautifulSoup, url: str, hours: str, now: int) -> dict:
    """料金の記録。料金表がない・読めないときは missing の付いた記録（料金なし）。"""
    root = soup.select_one("#price")
    if root is None:
        return missing_record(url, hours, now, "no-price-table")
    for comment in root.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()
    sections, skipped = parse_sections(root)
    if not any("30分" in unicodedata.normalize("NFKC", section["title"]) for section in sections):
        return missing_record(url, hours, now, "no-slot-table")
    links = []
    for anchor in root.select("a[href]"):
        link = urljoin(url, anchor.get("href", ""))
        if link not in links:
            links.append(link)
    record = {
        "hours": hours,
        "sections": sections,
        "notices": notices_of(root),
        "links": links,
        "page": url,
        "scope": "all",
        "checked_at": now,
    }
    if skipped:
        record["skipped"] = skipped
    # .tbl-wrap の外の表は読んでいない料金表かもしれない（期間料金など）
    extra = [table for table in root.find_all("table") if not table.find_parent(class_="tbl-wrap")]
    if extra:
        record["unread_tables"] = len(extra)
    return record


def missing_record(url: str, hours: str, now: int, reason: str) -> dict:
    return {"hours": hours, "sections": [], "missing": reason, "page": url, "scope": "all", "checked_at": now}


def shop_info(shop_id: str, url: str, soup: BeautifulSoup) -> dict | None:
    """店の一覧の行。店名・住所・営業時間・地図のどれかが読めなければ None。"""
    heading = next((h for h in soup.select("h1,h2")
                    if "メガビッグ" in clean(h.get_text(" ", strip=True))), None)
    if heading is None:
        return None
    name = clean(heading.get_text(" ", strip=True))
    name = re.sub(r"^カラオケ\s*メガビッグ\s*", "", name)
    address = unicodedata.normalize("NFKC", field_text(soup, "住所"))
    address = re.sub(r"〒?\s*\d{3}-\d{4}", "", address)
    address = re.sub(r"\s+", "", address)
    prefecture = PREFECTURE.match(address)
    hours = field_text(soup, "営業時間")
    map_frame = soup.select_one('iframe[src*="google.com/maps/embed"]')
    map_src = map_frame.get("src", "") if map_frame else ""
    coordinates = MAP_COORDINATES.search(map_src)
    if not name or not prefecture or not address or not hours or not coordinates:
        return None
    booking = next((urljoin(url, a.get("href", "")) for a in soup.select("a[href]")
                    if "WEB予約" in clean(a.get_text(" ", strip=True))
                    and urlsplit(urljoin(url, a.get("href", ""))).scheme == "https"), "")
    return {
        "id": PREFIX + shop_id,
        "chain": CHAIN,
        "name": name,
        "prefecture": prefecture.group(1),
        "address": address,
        "hours": hours,
        "lat": round(float(coordinates.group(2)), 6),
        "lng": round(float(coordinates.group(1)), 6),
        "url": url,
        "reserve": booking,
    }


def parse_shop(shop_id: str, url: str, markup: str, now: int | None = None) -> tuple[dict | None, dict]:
    """(店の一覧の行か None, 料金の記録) を返す。料金表が読めなければ missing の付いた記録。"""
    now = int(time.time()) if now is None else now
    soup = BeautifulSoup(markup, "html.parser")
    shop = shop_info(shop_id, url, soup)
    if shop is None:
        return None, missing_record(url, "", now, "unreadable-shop")
    return shop, price_record(soup, url, shop["hours"], now)


def shop_urls(markup: str) -> dict[str, str]:
    """一覧の店の ID と URL。読めなければ空。"""
    soup = BeautifulSoup(markup, "html.parser")
    found = {}
    for link in soup.select("a[href]"):
        absolute = urljoin(INDEX_URL, link.get("href", ""))
        if urlsplit(absolute).hostname != "megakara.jp":
            continue
        match = SHOP_PATH.fullmatch(urlsplit(absolute).path)
        if match:
            found[PREFIX + match.group(1)] = f"https://megakara.jp/shop/{match.group(1)}/"
    return dict(sorted(found.items()))


def closed(row: dict | None, now: int) -> bool:
    """店名の【YYYY/M/D閉店】の日付を過ぎた店（アプリも表示しない）。"""
    match = CLOSED.search((row or {}).get("name", ""))
    if not match:
        return False
    try:
        end = datetime(*map(int, match.groups()), tzinfo=JST) + timedelta(days=1)
    except ValueError:
        return False
    return now >= end.timestamp()


def read_json(path: Path, default: dict) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n",
                         encoding="utf-8")
    os.replace(temporary, path)


def update(log=print, fetcher: net.Fetcher | None = None, now: int | None = None) -> bool:
    """取得して保存する。通信の失敗がなければ True（締め切りで残した店は失敗に数えない）。"""
    now = int(time.time()) if now is None else now
    old_chains = read_json(CHAIN_FILE, {"schema_version": 1, "updated_at": 0, "shops": []})
    old_prices = read_json(PRICE_FILE, {"schema_version": 1, "updated_at": 0, "shops": {}})
    previous = {row["id"]: row for row in old_chains.get("shops", []) if row.get("chain") == CHAIN}
    prices = dict(old_prices.get("shops", {}))
    rows = dict(previous)
    fetcher = fetcher or net.Fetcher(USER_AGENT, label="メガビッグ")

    try:
        listed = shop_urls(fetch(fetcher, INDEX_URL))
    except net.OutOfTime:
        log("メガビッグ: 締め切りを過ぎたので取りません")
        return True
    except (Stopped, Failed, Gone, requests.RequestException) as error:
        log(f"メガビッグ: 店の一覧を取れないので前回の記録を残します ({error})")
        return False
    earlier = {key: row.get("url") or f"https://megakara.jp/shop/{key[len(PREFIX):]}/"
               for key, row in previous.items() if key in prices}
    vanished = sorted(key for key in prices if listed and key not in listed)
    removed: list[str] = []
    if not listed:
        log("メガビッグ: 店の一覧から店を読めないので、前回の店を取り直します")
        urls = earlier
    elif len(vanished) > MAX_REMOVED:
        log(f"メガビッグ: 一覧から {len(vanished)} 店が消えたので、一覧の読み違いとみなして消しません")
        urls = {**earlier, **listed}
    else:
        removed.extend(vanished)
        urls = dict(listed)
    for key in sorted(key for key in urls if closed(previous.get(key), now)):
        if key in prices and len(removed) < MAX_REMOVED:
            removed.append(key)
    for key in removed:
        prices.pop(key, None)
        log(f"メガビッグ {key}: 一覧にないか閉店したので料金の記録を消します（店の一覧の行は残します）")
    targets = sorted(key for key in urls if key not in removed and not closed(previous.get(key), now))

    # 店ごとに 3 日に 1 回、古い順に取る（crawl_plan.py）。表の外の文を残していない古い形の記録は毎回取る
    def stale_format(key: str) -> bool:
        record = prices.get(key)
        return record is not None and "missing" not in record and "notices" not in record

    due = crawl_plan.due(targets, prices.get, SOURCE, now, force=stale_format)
    failed = []
    for number, key in enumerate(due):
        time.sleep(DELAY)  # 一覧を読んだ直後も含め、リクエストの間を 1 秒以上あける
        url = urls[key]
        try:
            markup = fetch(fetcher, url)
        except net.OutOfTime:
            log(f"メガビッグ: 締め切りを過ぎたので残りの {len(due) - number} 店は次回に取ります")
            break
        except Stopped as error:
            failed.extend(due[number:])
            log(f"メガビッグ {key}: 公式サイトに断られたので残りの店は前回の記録を残します ({error})")
            break
        except Gone as error:
            if key in rows:  # 店の一覧にない店の料金の記録は作らない
                prices[key] = missing_record(url, (prices.get(key) or {}).get("hours", ""), now, "not-found")
            log(f"メガビッグ {key}: ページがないので料金なしにします ({error})")
            continue
        except (Failed, requests.RequestException) as error:
            failed.append(key)
            log(f"メガビッグ {key}: 通信の失敗のため前回の記録を残します ({type(error).__name__}: {error})")
            continue
        try:
            shop, record = parse_shop(key[len(PREFIX):], url, markup, now)
        except (ValueError, KeyError, AttributeError, TypeError) as error:
            shop, record = None, missing_record(url, "", now, "unreadable-shop")
            log(f"メガビッグ {key}: ページを読めません ({type(error).__name__}: {error})")
        if shop is not None:
            rows[key] = shop
        elif key not in rows:
            log(f"メガビッグ {key}: 新しい店の情報を読めないので今回は加えません")
            continue
        prices[key] = record
        if record.get("missing"):
            log(f"メガビッグ {key}: 料金表がないので料金なしにします ({record['missing']})")
        for item in record.get("skipped", []):
            log(f"メガビッグ {key}: 読めない表を捨てました ({item['time'] or item['title']}: {item['reason']})")

    chain_rows = [row for row in old_chains.get("shops", []) if row.get("chain") != CHAIN]
    chain_rows.extend(rows.values())
    chain_rows.sort(key=lambda row: (row.get("chain", ""), row.get("id", "")))
    write_json(CHAIN_FILE, {
        "schema_version": old_chains.get("schema_version", 1),
        "updated_at": old_chains.get("updated_at", 0) if failed else now,
        "shops": chain_rows,
    })
    write_json(PRICE_FILE, {
        "schema_version": 1,
        "updated_at": old_prices.get("updated_at", 0) if failed else now,
        "shops": dict(sorted(prices.items())),
    })
    readable = sum(1 for record in prices.values() if not record.get("missing"))
    log(f"メガビッグ: 今回 {len(due)} 店を確認、料金表 {readable}/{len(prices)} 店 (失敗 {len(failed)})")
    return not failed


def main() -> int:
    try:
        return 0 if update(log=lambda line: print(line, flush=True)) else 1
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(f"メガビッグの更新を中止: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
