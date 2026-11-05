#!/usr/bin/env python3
"""コート・ダジュールの公式料金JSONを取得して保存する。

店舗ページの JavaScript が /public/{shop_code}_price.json を取得して
料金表を表示するため、その同じJSONを店舗IDごとに保存する。
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

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "source" / "cotedazur_prices.json"
SHOPS_FILE = ROOT / "chains.json"
BASE_URL = "https://www.cotedazur.jp/public/"
USER_AGENT = "karaoke-auto-getter/1.0 (public price data; GitHub Actions)"
DELAY = 1.0
TIMEOUT = 30
MIN_RATIO = 0.8
SCHEMA_VERSION = 1
PRICE_GROUPS = ("noon_weekday", "noon_weekend", "night_weekday", "night_weekend")
PRICE_FIELD = re.compile(r"(?:_30|_tax|_normal|_student|_ordinary)$|^ftfd_")


class Throttled(Exception):
    pass


class NoPriceData(Exception):
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


def fetch_one(session: requests.Session, shop_id: str, shop: dict, previous: dict | None):
    code = shop_id.removeprefix("cd-")
    url = f"{BASE_URL}{code}_price.json"
    headers = {}
    if previous and previous.get("etag"):
        headers["If-None-Match"] = previous["etag"]
    if previous and previous.get("last_modified"):
        headers["If-Modified-Since"] = previous["last_modified"]
    time.sleep(DELAY)
    response = session.get(url, headers=headers, timeout=TIMEOUT)
    if response.status_code in (403, 429, 503):
        raise Throttled(f"HTTP {response.status_code}")
    if response.status_code == 404:
        raise NoPriceData("公式の料金JSONなし")
    if response.status_code == 304:
        if not previous:
            raise ValueError("初回取得で HTTP 304 が返りました")
        return {**previous, "checked_at": int(time.time())}
    response.raise_for_status()
    final = urlsplit(response.url)
    if final.scheme != "https" or final.hostname != "www.cotedazur.jp":
        raise ValueError(f"想定外の転送先: {response.url}")
    payload = response.json()
    validate_payload(shop_id, payload)
    return {
        **payload,
        "page": shop["url"],
        "etag": response.headers.get("ETag", ""),
        "last_modified": response.headers.get("Last-Modified", ""),
        "checked_at": int(time.time()),
    }


def update(log=print) -> bool:
    shops = load_shops()
    if not shops:
        raise ValueError("店舗一覧にコート・ダジュールがありません")
    previous_doc = load_previous()
    previous = previous_doc["shops"]
    current = {shop_id: previous[shop_id] for shop_id in shops if shop_id in previous}
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    verified = failures = unsupported = 0
    throttled = False

    for shop_id, shop in shops.items():
        old = previous.get(shop_id)
        try:
            current[shop_id] = fetch_one(session, shop_id, shop, old)
            verified += 1
        except Throttled as error:
            failures += len(shops) - verified - failures - unsupported
            log(f"  公式サイトの制限で停止: {error}")
            throttled = True
            break
        except NoPriceData as error:
            unsupported += 1
            if old:
                failures += 1
                current[shop_id] = old
                log(f"  料金JSONなし、前回値を保持 {shop_id} ({error})")
            else:
                log(f"  料金JSONなし、配信対象外 {shop_id} ({error})")
        except (requests.RequestException, ValueError, KeyError, TypeError) as error:
            failures += 1
            log(f"  取得失敗 {shop_id} ({error})")
            if old:
                current[shop_id] = old

    required = max(1, int(len(shops) * MIN_RATIO + 0.999))
    if verified < required:
        log(f"コート・ダジュール: 確認できた店が少ないため前回の値を残します ({verified}/{len(shops)}、必要 {required})")
        return False
    updated_at = previous_doc["updated_at"] if failures else int(time.time())
    save(current, updated_at)
    log(f"コート・ダジュール: {len(current)}/{len(shops)} 店 (確認 {verified}、対象外 {unsupported}、失敗 {failures})")
    return failures == 0 and not throttled


if __name__ == "__main__":
    try:
        ok = update(log=lambda line: print(line, flush=True))
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"::error::コート・ダジュール料金の更新に失敗: {error}", file=sys.stderr, flush=True)
        sys.exit(1)
    sys.exit(0 if ok else 1)
