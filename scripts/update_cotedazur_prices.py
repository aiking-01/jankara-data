#!/usr/bin/env python3
"""コート・ダジュールの公式料金JSONを取得して保存する。金額への変換は web/cotedazur.js が行う。

店舗ページの JavaScript が /public/{shop_code}_price.json を取得して
料金表を表示するため、その同じJSONを店舗IDごとに保存する。

- 店ごとに 3 日に 1 回、古い順に取る（crawl_plan.py）。前回と同じ JSON なら ETag で中身を受け取らない。
- タイムアウト、接続エラー、本文の欠け（Content-Length と長さが違う）は net.Fetcher が新しい接続で読み直す。
  それでもだめなとき、5xx、途中で切れた JSON は通信の失敗として前回の記録を残す
  （確かめた日時は進まないので、14 日で publish.py が配らなくなる）。通信の失敗が 5 店続いたら残りは取らない。
- 料金JSONがない（404）、または取れたのに読めないときは、前回の金額を残さず「料金なし」の記録にする。
- 403 / 429 / 503 は公式サイトの制限とみなして、残りの店は取らない。
- 締め切り（CRAWL_DEADLINE）を過ぎたら、残りの店は取らずに、取れた分を保存して終わる。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

import requests

import crawl_plan
import net

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "source" / "cotedazur_prices.json"
SHOPS_FILE = ROOT / "chains.json"
BASE_URL = "https://www.cotedazur.jp/public/"
USER_AGENT = "karaoke-auto-getter/1.0 (public price data; GitHub Actions)"
DELAY = 1.0
THROTTLED_CODES = (403, 429, 503)
MAX_STREAK = 5  # 通信の失敗がこの店数続いたら、残りの店は今回は取らない
SCHEMA_VERSION = 1
PRICE_GROUPS = ("noon_weekday", "noon_weekend", "night_weekday", "night_weekend")
PRICE_FIELD = re.compile(r"(?:_30|_tax|_normal|_student|_ordinary)$|^ftfd_")


class Throttled(Exception):
    pass


def load_shops() -> dict[str, dict]:
    doc = json.loads(SHOPS_FILE.read_text(encoding="utf-8"))
    records = doc["shops"].values() if isinstance(doc["shops"], dict) else doc["shops"]
    return {shop["id"]: shop for shop in records if shop.get("chain") == "コート・ダジュール"}


def load_previous() -> dict:
    if not DATA_FILE.exists():
        return {"schema_version": SCHEMA_VERSION, "updated_at": 0, "shops": {}}
    doc = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    if doc.get("schema_version") != SCHEMA_VERSION or not isinstance(doc.get("shops"), dict):
        raise ValueError("既存のコート・ダジュール料金データの形式が違います")
    return doc


def parse_amount(value, key: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{key}: 金額が boolean です")
    text = unicodedata.normalize("NFKC", str(value)).replace(",", "").strip()
    if text in ("", "-", "ー", "－"):
        return None
    if not re.fullmatch(r"\d+", text):
        raise ValueError(f"{key}: 金額を読み取れません ({value!r})")
    amount = int(text)
    limit = 20000 if key.endswith("_30") else 100000
    if amount > limit:
        raise ValueError(f"{key}: 金額が上限を超えています ({amount})")
    return amount


def price_values(value, path=""):
    if isinstance(value, dict):
        for key, item in value.items():
            next_path = f"{path}.{key}" if path else key
            if PRICE_FIELD.search(key):
                yield next_path, key, item
            elif isinstance(item, (dict, list)):
                yield from price_values(item, next_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from price_values(item, f"{path}[{index}]")


def validate_payload(shop_id: str, payload: dict) -> None:
    code = shop_id.removeprefix("cd-")
    if not re.fullmatch(r"\d{4,6}", code) or type(payload.get("shop_code")) is not int or payload["shop_code"] != int(code):
        raise ValueError("店舗コードが店舗IDと一致しません")
    name = payload.get("shop_name")
    if not isinstance(name, str) or not name.strip() or len(name) > 120 or "\ufffd" in name:
        raise ValueError("店舗名が不正です")
    plans = payload.get("price_plan")
    if not isinstance(plans, list) or not plans or len(plans) > 30:
        raise ValueError("price_plan が空、または想定より多すぎます")

    has_30_minute_price = False
    for index, plan in enumerate(plans):
        if not isinstance(plan, dict) or not isinstance(plan.get("label_name"), str):
            raise ValueError(f"price_plan[{index}] の形式が違います")
        for group in PRICE_GROUPS:
            rows = plan.get(group)
            if rows is None:
                continue
            if not isinstance(rows, list):
                raise ValueError(f"price_plan[{index}].{group} が配列ではありません")
            if any(not isinstance(row, dict) for row in rows):
                raise ValueError(f"price_plan[{index}].{group} に不正な行があります")

    for path, key, value in price_values(plans, "price_plan"):
        amount = parse_amount(value, path)
        if amount is not None and key.endswith("_30"):
            has_30_minute_price = True
    if not has_30_minute_price:
        raise ValueError("30分料金がありません")


def save(shops: dict, updated_at: int) -> None:
    payload = {"schema_version": SCHEMA_VERSION, "updated_at": updated_at,
               "shops": dict(sorted(shops.items()))}
    temp = DATA_FILE.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8", newline="")
    os.replace(temp, DATA_FILE)


def no_price(shop_id: str, shop: dict, reason: str) -> dict:
    """ページは取れたのに料金を読めない店の記録。前回の金額は残さない（アプリは「料金表を見る」になる）。"""
    return {
        "shop_code": int(shop_id.removeprefix("cd-")),
        "shop_name": shop.get("name") or shop_id,
        "price_plan": [],
        "no_price": reason[:120],
        "page": shop["url"],
        "checked_at": int(time.time()),
    }


def fetch_one(fetcher: net.Fetcher, shop_id: str, shop: dict, previous: dict | None) -> dict:
    """1 店の料金 JSON を取る。通信の失敗は requests の例外を投げる（呼び出し側が前回の記録を残す）。"""
    code = shop_id.removeprefix("cd-")
    url = f"{BASE_URL}{code}_price.json"
    headers = {}
    # 料金を読めた記録があるときだけ、変わっていなければ中身を受け取らない
    if previous and previous.get("price_plan"):
        if previous.get("etag"):
            headers["If-None-Match"] = previous["etag"]
        if previous.get("last_modified"):
            headers["If-Modified-Since"] = previous["last_modified"]
    time.sleep(DELAY)
    response = fetcher.get(url, headers=headers)
    status = response.status_code
    if status in THROTTLED_CODES:
        raise Throttled(f"HTTP {status}")
    if status >= 500:
        raise requests.HTTPError(f"HTTP {status}")
    if status == 304:
        if not headers:
            raise requests.HTTPError("前回の記録がないのに HTTP 304 が返りました")
        return {**previous, "checked_at": int(time.time())}
    if status == 404:
        return no_price(shop_id, shop, "公式の料金JSONなし")
    if status != 200:
        return no_price(shop_id, shop, f"料金JSONが HTTP {status}")
    final = urlsplit(getattr(response, "url", "") or url)
    if final.scheme != "https" or final.hostname != "www.cotedazur.jp":
        return no_price(shop_id, shop, f"想定外の転送先: {final.geturl()}")
    body = response.content.strip()
    if body.startswith(b"{") and not body.endswith(b"}"):
        # 長さの書かれていない応答が途中で切れたもの。読めない料金表ではなく通信の失敗として前回の記録を残す
        raise net.Incomplete(f"料金JSONが途中までしか届いていません ({len(body)} バイト)")
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("料金JSONの形式が違います")
        validate_payload(shop_id, payload)
    except (ValueError, KeyError, TypeError) as error:
        return no_price(shop_id, shop, f"料金JSONを読めません: {error}")
    return {
        **payload,
        "page": shop["url"],
        "etag": response.headers.get("ETag", ""),
        "last_modified": response.headers.get("Last-Modified", ""),
        "checked_at": int(time.time()),
    }


def update(log=print) -> bool:
    """今回取る店（crawl_plan.py）を取り直して保存する。通信に失敗した店だけ前回の記録を残す。"""
    shops = load_shops()
    if not shops:
        raise ValueError("店舗一覧にコート・ダジュールがありません")
    previous_doc = load_previous()
    previous = previous_doc["shops"]
    current = {shop_id: previous[shop_id] for shop_id in shops if shop_id in previous}
    now = int(time.time())
    # 店ごとに 3 日に 1 回、古い順に取る（crawl_plan.py）
    due = crawl_plan.due(list(shops), previous.get, "source/cotedazur_prices.json", now)
    fetcher = net.Fetcher(USER_AGENT, label="コート・ダジュール")
    verified = unsupported = failures = streak = 0
    stopped = ""
    for index, shop_id in enumerate(due):
        if streak >= MAX_STREAK:
            stopped = f"通信の失敗が {streak} 店続いたので、残りの {len(due) - index} 店は今回は取りません"
            failures += len(due) - index
            break
        old = previous.get(shop_id)
        try:
            record = fetch_one(fetcher, shop_id, shops[shop_id], old)
        except net.OutOfTime:
            stopped = f"締め切りを過ぎたので、残りの {len(due) - index} 店は今回は取りません"
            break
        except Throttled as error:
            stopped = f"公式サイトの制限で停止 ({error})。残りの {len(due) - index} 店は今回は取りません"
            failures += len(due) - index
            break
        except requests.RequestException as error:
            failures += 1
            streak += 1
            log(f"  取得失敗、前回の記録を残します {shop_id} ({type(error).__name__}: {error})")
            continue
        streak = 0
        current[shop_id] = record
        if record.get("no_price"):
            unsupported += 1
            log(f"  料金なし {shop_id} ({record['no_price']})")
        else:
            verified += 1
    if stopped:
        log(f"  {stopped}")
    complete = not failures and not stopped
    save(current, int(time.time()) if complete else previous_doc["updated_at"])
    log(f"コート・ダジュール: {len(current)}/{len(shops)} 店 (今回 {len(due)} 店: 確認 {verified}、料金なし {unsupported}、"
        f"失敗 {failures}、通信 {fetcher.bytes / 1e6:.1f}MB)")
    return failures == 0


if __name__ == "__main__":
    try:
        ok = update(log=lambda line: print(line, flush=True))
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"::error::コート・ダジュール料金の更新に失敗: {error}", file=sys.stderr, flush=True)
        sys.exit(1)
    sys.exit(0 if ok else 1)
