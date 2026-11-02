#!/usr/bin/env python3
"""まねきねこの料金表 PDF の文字を座標つきで取り出して保存する。金額への変換は web/maneki.js が行う。

PDF は複数ページのものが多く、料金表のページだけを残す。店の URL は /locations/ の「もっと見る」と同じ一覧から得る。

robots.txt は PDF の置き場所 /wp-content/uploads/ を巡回の対象外としているが、利用者の判断で全店を確認する。
負荷を抑えるため 1 秒に 1 件ずつ順に取得し、店のページの更新日時が変わった店の PDF だけを確認する。

使い方:
    python update_maneki_prices.py                  # 全店更新（約 15 分。変わった店の数で増える）
    python update_maneki_prices.py --from-cache DIR # 保存済みの mn-*.html と mn-*.pdf から作る
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import requests

import update_chains

try:
    import pypdfium2
except ImportError:  # PC 版は PDF を読めないときも他のチェーンの更新を続ける
    pypdfium2 = None

DATA_FILE = Path(__file__).resolve().parents[1] / "source" / "maneki_prices.json"
USER_AGENT = update_chains.USER_AGENT
SCHEMA_VERSION = 1
DELAY = 1.0
THROTTLED_CODES = (403, 429, 503)
ATTEMPTS = 2
RECHECK_SECONDS = 20 * 3600
MIN_RATIO = 0.8
SAVE_EVERY = 20
CHUNK = 4096
MAX_PDF_BYTES = 20 * 1024 * 1024
OPTION_HEAD = "オプション"
FOOTNOTE_START = "※ご利用は"
NOISE = ("円", "~", "～")
MODIFIED = re.compile(r'article:modified_time" content="([^"]+)"')
PDF_URL = re.compile(r"https://www\.karaokemanekineko\.jp/wp-content/uploads/[^\"'\s]+\.pdf")


class Throttled(Exception):
    pass


def char_runs(page) -> list[list]:
    """1 ページ目の文字を、空白で切れ目のない並びごとにまとめる。"""
    height = page.get_height()
    text = page.get_textpage()
    runs: list[list] = []
    current = None
    for index in range(text.count_chars()):
        char = text.get_text_range(index, 1)
        if not char or char.isspace():
            current = None
            continue
        left, bottom, right, top = text.get_charbox(index)
        y0, y1 = height - top, height - bottom
        size = y1 - y0
        joins = (
            current is not None
            and -2 < left - current[3] < max(4, size * 0.6)
            and abs((y0 + y1) / 2 - (current[2] + current[4]) / 2) < size * 0.6
        )
        if joins:
            current[0] += char
            current[3] = max(current[3], right)
            current[2] = min(current[2], y0)
            current[4] = max(current[4], y1)
        else:
            current = [char, left, y0, right, y1]
            runs.append(current)
    return runs


def keep_tokens(runs: list[list]) -> list[list]:
    """オプションの欄より上の文字と末尾の注記を、描かれた順のまま返す。"""
    option_y = min((run[2] for run in runs if run[0] == OPTION_HEAD), default=None)
    note_y = min((run[2] for run in runs if run[0].startswith(FOOTNOTE_START)), default=None)
    kept = [
        run for run in runs
        if run[0] not in NOISE
        and (option_y is None or run[2] < option_y or (note_y is not None and run[2] >= note_y - 2))
    ]
    return [[run[0], round(run[1], 1), round(run[2], 1), round(run[3], 1), round(run[4], 1)] for run in kept]


def is_price_table(runs: list[list]) -> bool:
    words = [unicodedata.normalize("NFKC", run[0]) for run in runs]
    return "30分" in words and any("会員" in word for word in words)


def tables_of(pdf: bytes | str | Path) -> list[list]:
    """料金表のページの文字をページごとに返す。文字のない PDF は空。"""
    document = pypdfium2.PdfDocument(pdf if isinstance(pdf, bytes) else str(pdf))
    try:
        pages = [char_runs(document[index]) for index in range(len(document))]
    finally:
        document.close()
    return [keep_tokens(runs) for runs in pages if is_price_table(runs)]


def load_previous() -> dict[str, dict]:
    if not DATA_FILE.exists():
        return {}
    return json.loads(DATA_FILE.read_text(encoding="utf-8")).get("shops", {})


def save(shops: dict[str, dict], updated_at: int) -> None:
    payload = json.dumps({"schema_version": SCHEMA_VERSION, "updated_at": updated_at, "shops": shops},
                         ensure_ascii=False, separators=(",", ":"))
    temp = DATA_FILE.with_suffix(".json.tmp")
    temp.write_text(payload, encoding="utf-8", newline="")
    os.replace(temp, DATA_FILE)


def last_updated() -> int:
    if not DATA_FILE.exists():
        return 0
    return json.loads(DATA_FILE.read_text(encoding="utf-8")).get("updated_at", 0)


def shop_pages() -> dict[str, str]:
    return {shop["id"]: shop["url"] for shop in update_chains.load_previous().values() if shop["chain"] == "まねきねこ"}


def check(response: requests.Response) -> None:
    if response.status_code in THROTTLED_CODES:
        raise Throttled(f"HTTP {response.status_code}")
    response.raise_for_status()


def read_page(session: requests.Session, url: str, known: str | None) -> tuple[str, str | None, int]:
    """店のページを必要なところまで読む。更新日時が前回と同じなら PDF の場所まで読まずに切る。"""
    time.sleep(DELAY)
    with session.get(url, timeout=30, stream=True) as response:
        check(response)
        text = ""
        size = 0
        modified = None
        for chunk in response.iter_content(CHUNK):
            size += len(chunk)
            text += chunk.decode("utf-8", "ignore")
            if modified is None and (match := MODIFIED.search(text)):
                modified = match.group(1)
                if modified == known:
                    return modified, None, size
            if match := PDF_URL.search(text):
                return modified or "", match.group(0), size
    return modified or "", None, size


def read_pdf(session: requests.Session, url: str, etag: str | None) -> tuple[bytes | None, str | None]:
    """前回と同じ PDF なら本文は受け取らない。"""
    time.sleep(DELAY)
    headers = {"If-None-Match": etag} if etag else {}
    response = session.get(url, timeout=60, headers=headers)
    if response.status_code == 304:
        return None, etag
    check(response)
    if response.content[:4] != b"%PDF" or len(response.content) > MAX_PDF_BYTES:
        raise ValueError("PDF ではありません")
    return response.content, response.headers.get("ETag")


def fetch_shop(session: requests.Session, url: str, old: dict) -> tuple[dict, int]:
    """1 店を確認して、保存する内容と通信量を返す。"""
    now = int(time.time())
    modified, pdf_url, size = read_page(session, url, old.get("modified") if old.get("tables") is not None else None)
    if pdf_url is None:
        # 更新日時が同じか、料金表のない店
        same = modified and modified == old.get("modified")
        return ({**old, "checked_at": now} if same else {"modified": modified, "checked_at": now, "tables": []}), size
    same_url = pdf_url == old.get("pdf")
    body, etag = read_pdf(session, pdf_url, old.get("etag") if same_url else None)
    if body is None:
        return {**old, "modified": modified, "checked_at": now}, size
    return {"pdf": pdf_url, "etag": etag, "modified": modified, "checked_at": now, "tables": tables_of(body)}, size + len(body)


def readable(shops: dict[str, dict]) -> int:
    return sum(1 for shop in shops.values() if shop.get("tables"))


def accept(previous: dict[str, dict], shops: dict[str, dict]) -> bool:
    return readable(shops) >= readable(previous) * MIN_RATIO


def update(log=print) -> bool:
    """更新して保存する。失敗した店は前回の値を残す。全店を確認できたら True。"""
    if pypdfium2 is None:
        log("pypdfium2 がないのでまねきねこの料金を更新できません")
        return False
    previous = load_previous()
    shops = dict(previous)
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    pages = shop_pages()
    now = int(time.time())
    due = [shop_id for shop_id in pages if now - shops.get(shop_id, {}).get("checked_at", 0) >= RECHECK_SECONDS]
    failed = 0
    total = 0
    for number, shop_id in enumerate(due, 1):
        for attempt in range(ATTEMPTS):
            try:
                shops[shop_id], size = fetch_shop(session, pages[shop_id], shops.get(shop_id, {}))
                total += size
                break
            except Throttled as stop:
                log(f"公式サイトに制限されたので中止します ({stop})")
                if accept(previous, shops):
                    save(shops, last_updated())
                return False
            except (requests.RequestException, ValueError) as failure:
                if attempt == ATTEMPTS - 1:
                    log(f"  取得失敗 {shop_id} ({failure})")
                    failed += 1
        if number % SAVE_EVERY == 0 and accept(previous, shops):
            save(shops, last_updated())
    shops = {shop_id: shops[shop_id] for shop_id in pages if shop_id in shops}
    if not accept(previous, shops):
        log(f"文字を読めた店が異常に少ないので前回の値を残します ({readable(previous)} → {readable(shops)})")
        return False
    save(shops, last_updated() if failed else int(time.time()))
    log(f"まねきねこ: {readable(shops)}/{len(pages)} 店 (失敗 {failed}, 通信 {total / 1e6:.1f}MB)")
    return not failed


def from_cache(directory: Path) -> None:
    now = int(time.time())
    index_path = directory / "index_all.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
    shops = {}
    for shop_id in shop_pages():
        path = directory / f"{shop_id}.pdf"
        page = directory / f"{shop_id}.html"
        if not path.exists() or not page.exists():
            continue
        html = page.read_text(encoding="utf-8", errors="ignore")
        modified = MODIFIED.search(html)
        row = index.get(shop_id, {})
        found = PDF_URL.search(html)
        shops[shop_id] = {
            "pdf": row.get("url") or (found.group(0) if found else ""),
            "etag": row.get("etag"),
            "modified": modified.group(1) if modified else "",
            "checked_at": now,
            "tables": tables_of(path),
        }
    save(shops, now)
    print(f"{len(shops)} 店を保存しました ({readable(shops)} 店は文字あり)")


def main() -> int:
    parser = argparse.ArgumentParser(description="まねきねこの料金データ更新")
    parser.add_argument("--from-cache", type=Path, help="保存済みの HTML と PDF のフォルダから作る")
    args = parser.parse_args()
    if args.from_cache:
        from_cache(args.from_cache)
        return 0
    return 0 if update(log=lambda line: print(line, flush=True)) else 1


if __name__ == "__main__":
    sys.exit(main())
