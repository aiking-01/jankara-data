#!/usr/bin/env python3
"""ビッグエコーの料金表（30 分料金、フリータイム、パック）と営業時間を公式サイトから取得して保存する。

金額への変換は画面側 (web/bigecho.js) の 1 箇所で行うので、ここでは表をそのまま取り出すだけにする。

使い方:
    python update_chain_prices.py                  # 全店更新（約 2 分）
    python update_chain_prices.py --from-cache DIR # 保存済みの HTML (be-XXXX.html) から作る
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from bs4 import BeautifulSoup

import update_chains

DATA_FILE = Path(__file__).resolve().parents[1] / "source" / "chain_prices.json"
USER_AGENT = update_chains.USER_AGENT
SCHEMA_VERSION = 1
DELAY = 1.0 # スレッドごとの間隔
PARALLEL = 4 # 利用者の要望で同時に取得する
THROTTLED_CODES = (429, 503) # 公式サイトに制限されたら、残りの店は取りに行かない
ATTEMPTS = 2
# これより新しい店は取り直さない。中断したときは続きから進められる
RECHECK_SECONDS = 20 * 3600
# 表を読めた店が前回よりこの割合を下回ったら、サイト変更とみなして前回の値を残す
MIN_RATIO = 0.8
SAVE_EVERY = 20
# 保存した表の範囲。30 分料金だけを保存していた頃の店は、日時に関係なく取り直す
SCOPE = "all"
POINT_API = "https://shop.big-echo.jp/api/point/"
ALLOWED_PAGE_HOSTS = ("https://big-echo.jp/", "https://shop.big-echo.jp/")


class Throttled(Exception):
    pass


def clean(text: str | None) -> str:
    return " ".join((text or "").split())


def parse_page(html: str) -> dict | None:
    """料金欄の表と営業時間を取り出す。料金欄がなければ None。"""
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("#price")
    if root is None:
        return None
    sections = []
    heading = time_text = ""
    for node in root.select("h3,h4,table"):
        if node.name == "h3":
            heading, time_text = clean(node.get_text()), ""
        elif node.name == "h4":
            time_text = clean(node.get_text())
        else:
            rows = [[clean(cell.get_text()) for cell in tr.select("th,td")] for tr in node.select("tr")]
            body = [row for row in rows if len(row) > 1]
            if not body:
                continue
            single = [row[0] for row in rows if len(row) == 1 and row[0]]
            title = single[0] if single else ""
            sections.append({"heading": heading, "time": time_text, "title": title, "note": " ".join(single[1:]),
                             "header": body[0], "rows": body[1:]})
    label = next((dt for dt in soup.find_all("dt") if "営業時間" in dt.get_text()), None)
    value = label.find_next_sibling("dd") if label else None
    return {"hours": clean(value.get_text()) if value else "", "sections": sections}


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


def shop_ids() -> list[str]:
    return [shop["id"] for shop in update_chains.load_previous().values() if shop["chain"] == "ビッグエコー"]


def check(response: requests.Response) -> None:
    if response.status_code in THROTTLED_CODES:
        raise Throttled(f"HTTP {response.status_code}")
    response.raise_for_status()


def page_url(session: requests.Session, shop_id: str, known: str | None) -> str:
    if known:
        return known
    time.sleep(DELAY)
    response = session.get(POINT_API + shop_id.removeprefix("be-"), timeout=20)
    check(response)
    url = response.json()["短縮ＵＲＬ"]
    if not url.startswith(ALLOWED_PAGE_HOSTS):
        raise ValueError(f"想定外の接続先: {url}")
    return url


def fetch_shop(session: requests.Session, shop_id: str, known: str | None) -> dict | None:
    """1 店を読む。料金欄がなければ None。"""
    url = page_url(session, shop_id, known)
    time.sleep(DELAY)
    response = session.get(url, timeout=20)
    check(response)
    response.encoding = "utf-8"
    parsed = parse_page(response.text)
    if parsed is None:
        return None
    # 転送の先を覚えて、次回から 1 回の通信で済ませる
    final = response.url.replace("http://", "https://", 1)
    return {**parsed, "page": final if final.startswith(ALLOWED_PAGE_HOSTS) else url, "scope": SCOPE, "checked_at": int(time.time())}


def has_tables(shops: dict[str, dict]) -> int:
    return sum(1 for shop in shops.values() if shop.get("sections"))


def accept(previous: dict[str, dict], shops: dict[str, dict]) -> bool:
    return has_tables(shops) >= has_tables(previous) * MIN_RATIO


def update(log=print) -> bool:
    """更新して保存する。全店を読めて値が妥当なら True。失敗した店は前回の値を残す。"""
    previous = load_previous()
    shops = dict(previous)
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    failed = 0
    now = int(time.time())
    done = 0
    ids = shop_ids()
    due = [shop_id for shop_id in ids if now - shops.get(shop_id, {}).get("checked_at", 0) >= RECHECK_SECONDS
           or shops.get(shop_id, {}).get("scope") != SCOPE]
    throttled = threading.Event()

    def fetch(shop_id: str) -> tuple[str, dict | None, str]:
        if throttled.is_set():
            return shop_id, None, "公式サイトの制限で中止"
        error = ""
        for _ in range(ATTEMPTS):
            try:
                shop = fetch_shop(session, shop_id, shops.get(shop_id, {}).get("page"))
            except Throttled as stop:
                throttled.set()
                return shop_id, None, str(stop)
            except requests.RequestException as failure:
                error = str(failure)  # 一時的な 502 などがあるので取り直す
            except (ValueError, KeyError) as failure:
                return shop_id, None, str(failure)
            else:
                return shop_id, shop, "" if shop else "料金欄なし"
        return shop_id, None, error

    # 取得だけを同時に行い、結果はこのスレッドで順に反映する
    with ThreadPoolExecutor(PARALLEL) as pool:
        for shop_id, shop, error in pool.map(fetch, due):
            if shop is None:
                log(f"  取得失敗 {shop_id} ({error})")
                failed += 1
                continue
            shops[shop_id] = shop
            done += 1
            if done % SAVE_EVERY == 0 and accept(previous, shops):
                save(shops, last_updated())
    # 閉店した店は残さない
    shops = {shop_id: shops[shop_id] for shop_id in ids if shop_id in shops}
    if not accept(previous, shops):
        log(f"表を読めた店が異常に少ないので前回の値を残します ({has_tables(previous)} → {has_tables(shops)})")
        return False
    # 失敗した店があれば「成功した更新日時」は進めない
    save(shops, last_updated() if failed else int(time.time()))
    log(f"ビッグエコー: {has_tables(shops)}/{len(ids)} 店 (失敗 {failed})")
    return not failed


def from_cache(directory: Path) -> None:
    now = int(time.time())
    shops = {}
    for shop_id in shop_ids():
        path = directory / f"{shop_id}.html"
        parsed = parse_page(path.read_text(encoding="utf-8")) if path.exists() else None
        if parsed is not None:
            shops[shop_id] = {**parsed, "scope": SCOPE, "checked_at": now}
    save(shops, now)
    print(f"{len(shops)} 店を保存しました")


def main() -> int:
    parser = argparse.ArgumentParser(description="ビッグエコーの料金データ更新")
    parser.add_argument("--from-cache", type=Path, help="保存済みの HTML のフォルダから作る")
    args = parser.parse_args()
    if args.from_cache:
        from_cache(args.from_cache)
        return 0
    return 0 if update(log=lambda line: print(line, flush=True)) else 1


if __name__ == "__main__":
    sys.exit(main())
