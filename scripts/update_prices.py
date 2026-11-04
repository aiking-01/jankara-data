#!/usr/bin/env python3
"""ジャンカラ各店の料金表を取得して data/ に保存する。

https://jankara.ne.jp/shop/<number>/ の <number> を 1 から順に試し、
200 が返った店舗の料金を保存する。店舗番号は連番ではないため、
見つからない番号が --stop-after 回続いた時点で走査を終える。
料金が前回と変わった店舗には changed_at を記録する。

使い方:
    python update_prices.py                 # 全店更新
    python update_prices.py --start 100 --end 110 --dry-run
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://jankara.ne.jp/shop/{}/"
USER_AGENT = "jankara-price-navi/1.0 (personal price comparison)"
DATA_DIR = Path(__file__).resolve().parents[1] / "source"
SCHEMA_VERSION = 1
# 有効な店舗数・料金セル数が前回からこの割合を下回ったら、サイト変更とみなして保存しない
MIN_SHOP_RATIO = 0.95
MIN_PRICE_RATIO = 0.90
# この回数の全巡回で連続して消えていたら閉店扱いにする
CLOSED_AFTER_MISSES = 3
RETRIES = 3
ALERT_WORDS = re.compile("特別料金|年末年始|お盆|GW|ゴールデンウィーク|繁忙|特定日")
# 料金比較に関わる項目。ここが変わったら「料金変更」とみなす
PRICE_KEYS = ("regular", "solo")


class PageError(Exception):
    """200 が返ったのに店舗ページではなかった（メンテナンス画面など）。"""


class RateLimited(Exception):
    """サイトから 429 が返った。巡回を打ち切る。"""


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def own_text(tag) -> str:
    """子要素を除いた、タグ直下の最初の文字列。"""
    for text in tag.find_all(string=True, recursive=False):
        if text.strip():
            return clean(text)
    return ""


def parse_price(cell) -> int | None:
    digits = re.sub(r"\D", "", cell.get_text())
    return int(digits) if digits else None


def parse_table(table) -> dict:
    title = table.select_one("th.price_title")
    band = own_text(title) if title else ""
    if not band and title:
        band = clean(title.get_text(" ").split("ドリンク")[0])
    customers = [clean(th.get_text()) for th in table.select("th.attribute")]
    if not customers or len(customers) != len(set(customers)):
        raise PageError("料金表の区分見出しが欠損または重複しています")

    plans = []
    plan = None
    for row in table.find_all("tr"):
        if "price_name" in (row.get("class") or []):
            continue
        time_cell = row.find("td", class_="price_time")
        if time_cell:
            notes = [clean(p.get_text()) for p in time_cell.find_all("p")]
            plan = {
                "name": own_text(time_cell),
                "hours": next((n for n in notes if not n.startswith("最終受付")), ""),
                "last_entry": next(
                    (n.replace("最終受付", "").strip() for n in notes if n.startswith("最終受付")),
                    "",
                ),
                "days": [],
            }
            plans.append(plan)
        cells = row.find_all("td", class_="price")
        if plan is None or not cells:
            continue
        if len(cells) != len(customers):
            raise PageError(f"料金表の列数が不一致: {len(customers)} 区分 / {len(cells)} 金額")
        day = row.find("td", class_=["wd", "hd"])
        plan["days"].append(
            {
                "day": clean(day.get_text()) if day else "全日",
                "prices": dict(zip(customers, map(parse_price, cells))),
            }
        )

    area = table.find_parent(class_="price_table_area")
    note = area.find("p", class_="price_note") if area else None
    return {
        "band": band,
        "customers": customers,
        "plans": plans,
        "note": clean(note.get_text()) if note else "",
    }


def parse_sjk_table(section) -> dict:
    """スーパージャンカラの料金表。<table> ではなく div で組まれている。"""
    title = section.select_one(".sjkprice__header--title")
    customers = [clean(div.get_text()) for div in
                 section.select_one(".sjkprice__wrapper--header").select(".sjkprice__header")]

    plans = []
    for course in section.select(".sjkprice__wrapper--course"):
        cell = course.select_one(".sjkprice__cell--course")
        hours, _, last_entry = " ".join(clean(p.get_text()) for p in cell.find_all("p")).partition("最終受付")
        days = [clean(d.get_text()) for d in course.select(
            ".sjkprice__cell--blue, .sjkprice__cell--pink")]
        price_row = course.find_next_sibling(class_="sjkprice__wrapper--price")
        if price_row is None or not customers or not days:
            raise PageError("スーパージャンカラの料金表の見出し・金額がありません")
        prices = list(map(parse_price, price_row.select(".sjkprice__cell--price")))
        if len(prices) != len(days) * len(customers):
            raise PageError(f"スーパージャンカラの列数が不一致: 予想 {len(days) * len(customers)} / 実際 {len(prices)}")
        plans.append({
            "name": own_text(cell),
            "hours": hours.strip(),
            "last_entry": last_entry.strip(),
            "days": [
                {
                    "day": day,
                    "prices": dict(zip(customers, prices[i * len(customers):])),
                }
                for i, day in enumerate(days)
            ],
        })

    note = section.find("p", class_="price_note")
    return {
        "band": clean(title.get_text()).split(" ")[0] if title else "",
        "customers": customers,
        "plans": plans,
        "note": clean(note.get_text()) if note else "",
    }


def parse_shop(number: int, html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one("h1.shopinfo__heading")
    address = soup.select_one("address.shopinfo__detail-address")
    hours = soup.select_one("i.fa-clock-o")
    hours = hours.find_next("p") if hours else None

    prefecture = street = ""
    lat = lng = None
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            info = json.loads(script.string or "")
        except json.JSONDecodeError:
            continue
        if isinstance(info, dict) and isinstance(info.get("address"), dict):
            prefecture = info["address"].get("addressRegion", "")
            street = info["address"].get("streetAddress", "")
            geo = info.get("geo") or {}
            lat, lng = geo.get("latitude"), geo.get("longitude")
            break

    access = ""
    if address:
        lines = [clean(s) for s in address.stripped_strings]
        access = lines[-1] if len(lines) >= 3 else ""

    regular, solo = [], []
    for table in soup.select("table.price_table"):
        target = solo if table.find_parent(id="price-special") else regular
        target.append(parse_table(table))
    regular.extend(parse_sjk_table(section) for section in soup.select("div.sjkprice"))

    # 料金表の外に書かれた特別料金の告知を拾う（表は解析済みなので取り除く）
    alerts = []
    section = soup.find(id="price")
    if section:
        for tag in section.find_all(["style", "script", "table"]):
            tag.decompose()
        alerts = sorted(set(ALERT_WORDS.findall(section.get_text())))

    return {
        "id": number,
        "name": clean(heading.get_text()) if heading else "",
        "url": BASE_URL.format(number),
        "prefecture": prefecture,
        "address": street,
        "access": access,
        "hours": clean(hours.get_text()) if hours else "",
        "lat": lat,
        "lng": lng,
        "alerts": alerts,
        "regular": regular,
        "solo": solo,
    }


def fetch(session: requests.Session, number: int, cache_dir: Path | None,
          timeout: int = 30, retries: int = RETRIES) -> str | None:
    """店舗の HTML を返す。店舗が無ければ None、通信失敗なら例外。"""
    cached = cache_dir / f"{number}.html" if cache_dir else None
    if cached and cached.exists():
        return cached.read_text(encoding="utf-8")
    missing = cached.with_suffix(".missing") if cached else None
    if missing and missing.exists():
        return None

    url = BASE_URL.format(number)
    for attempt in range(retries):
        try:
            # 休止番号は 302 で店舗検索へ飛ぶので、リダイレクトは追わない
            response = session.get(url, timeout=timeout, allow_redirects=False)
        except requests.RequestException:
            if attempt == retries - 1:
                raise
        else:
            if response.status_code == 429:
                raise RateLimited(url)
            if response.status_code < 500:
                break
            if attempt == retries - 1:
                response.raise_for_status()
        time.sleep(5 * (attempt + 1))

    if response.status_code in (301, 302, 303, 307, 308, 404, 410):
        if missing:
            missing.touch()
        return None
    if response.status_code != 200:  # 403 などを未使用番号と誤認すると閉店扱いになる
        raise requests.HTTPError(f"HTTP {response.status_code}")
    response.encoding = "utf-8"
    if cached:
        cached.write_text(response.text, encoding="utf-8")
    return response.text


def load_previous() -> dict[int, dict]:
    path = DATA_DIR / "prices.json"
    if not path.exists():
        return {}
    return {shop["id"]: shop for shop in json.loads(path.read_text(encoding="utf-8"))["shops"]}


def now_epoch() -> int:
    return int(time.time())


def count_prices(shop: dict) -> int:
    return sum(
        1
        for table in shop["regular"] + shop["solo"]
        for plan in table["plans"]
        for day in plan["days"]
        for price in day["prices"].values()
        if price is not None
    )


def is_valid(shop: dict) -> bool:
    """店舗名・位置情報があり、通常料金が 1 つでも読めている。"""
    has_regular_price = any(
        price is not None
        for table in shop["regular"]
        for plan in table["plans"]
        for day in plan["days"]
        for price in day["prices"].values()
    )
    return bool(shop["name"] and shop["lat"] and shop["lng"] and has_regular_price)


def stamp(shop: dict, previous: dict | None, checked_at: int) -> bool:
    """確認日時と料金変更日時を記録し、料金が変わったかを返す。"""
    changed = bool(previous) and any(shop[k] != previous.get(k) for k in PRICE_KEYS)
    shop["checked_at"] = checked_at
    shop["changed_at"] = checked_at if changed else (previous or {}).get("changed_at", 0)
    shop["missing_strike"] = 0
    shop["closed"] = False
    return changed


def parse_page(number: int, html: str) -> dict:
    shop = parse_shop(number, html)
    if not is_valid(shop):
        raise PageError(f"{number}: 店舗ページとして解析できません")
    return shop


def newer(candidate: dict, current: dict | None, now: int) -> bool:
    """checked_at が新しい方を残す。端末時刻の異常で未来になった値は信用しない。"""
    if current is None:
        return True
    current_at = current.get("checked_at", 0)
    if current_at > now + 3600:
        return True
    return candidate.get("checked_at", 0) >= current_at


def write_atomic(path: Path, text: str) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8", newline="")
    os.replace(temp, path)


def csv_rows(shops: list[dict]):
    yield ["店舗番号", "店舗名", "都道府県", "料金種別", "時間帯", "プラン", "利用時間",
           "最終受付", "曜日", "区分", "料金(税込)"]
    for shop in shops:
        for kind, tables in (("通常", shop["regular"]), ("1名", shop["solo"])):
            for table in tables:
                for plan in table["plans"]:
                    for day in plan["days"]:
                        for customer, price in day["prices"].items():
                            yield [shop["id"], shop["name"], shop["prefecture"], kind,
                                   table["band"], plan["name"], plan["hours"],
                                   plan["last_entry"], day["day"], customer,
                                   "" if price is None else price]


def save(shops: list[dict], updated_at: int) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    payload = json.dumps({"schema_version": SCHEMA_VERSION, "updated_at": updated_at, "shops": shops},
                         ensure_ascii=False, separators=(",", ":"))
    write_atomic(DATA_DIR / "prices.json", payload)

    temp = DATA_DIR / "prices.csv.tmp"
    with temp.open("w", encoding="utf-8-sig", newline="") as file:  # Excel 向けに BOM 付き
        csv.writer(file).writerows(csv_rows(shops))
    os.replace(temp, DATA_DIR / "prices.csv")


def sweep(start: int = 1, end: int = 2000, stop_after: int = 60, delay: float = 1.0,
          cache_dir: Path | None = None, log=print,
          on_progress=None) -> tuple[dict[int, dict], list[int], list[int]]:
    """番号を順に取得し、(店舗, 取得失敗の番号, 未使用の番号) を返す。"""
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    previous = load_previous()
    known_max = max(previous, default=0)
    checked_at = now_epoch()

    shops: dict[int, dict] = {}
    failed: list[int] = []
    missing: list[int] = []
    misses = 0
    for number in range(start, end + 1):
        if on_progress:
            on_progress(number)
        try:
            html = fetch(session, number, cache_dir)
            shop = parse_page(number, html) if html is not None else None
        except (requests.RequestException, PageError) as error:
            log(f"{number:>4}: 取得失敗 ({error})")
            failed.append(number)
            misses = 0
        else:
            if shop is None:
                missing.append(number)
                misses += 1
                # 既知の番号より先でだけ打ち切る（途中に大きな欠番があっても止まらない）
                if misses >= stop_after and number > known_max:
                    log(f"{stop_after} 件連続で未使用のため {number} で終了")
                    break
            else:
                misses = 0
                if stamp(shop, previous.get(number), checked_at):
                    log(f"{number:>4}: 料金変更あり {shop['name']}")
                shops[number] = shop
                log(f"{number:>4}: {shop['name']} ({shop['prefecture']}) "
                    f"通常{len(shop['regular'])}表 / 1名{len(shop['solo'])}表")
        if not (cache_dir and (cache_dir / f"{number + 1}.html").exists()):
            time.sleep(delay)
    return shops, failed, missing


def merge(current: dict[int, dict], fetched: dict[int, dict], missing: list[int],
          now: int) -> dict[int, dict]:
    """巡回結果を現在のデータへマージする。取得できなかった店は前回の値を残す。"""
    merged = dict(current)
    for number, shop in fetched.items():
        if newer(shop, merged.get(number), now):
            merged[number] = shop
    for number in missing:
        if number in merged and number not in fetched:
            shop = dict(merged[number])
            shop["missing_strike"] = shop.get("missing_strike", 0) + 1
            shop["closed"] = shop["missing_strike"] >= CLOSED_AFTER_MISSES
            merged[number] = shop
    return merged


def validate(new: dict[int, dict], old: dict[int, dict]) -> None:
    """正常なデータを壊れたデータで上書きしないための検証。異常なら ValueError。"""
    new_shops = [s for s in new.values() if not s.get("closed") and is_valid(s)]
    old_shops = [s for s in old.values() if not s.get("closed") and is_valid(s)]
    if not new_shops or len(new_shops) < len(old_shops) * MIN_SHOP_RATIO:
        raise ValueError(f"有効な店舗数が異常です (前回 {len(old_shops)} → 今回 {len(new_shops)})")
    new_prices = sum(map(count_prices, new_shops))
    old_prices = sum(map(count_prices, old_shops))
    if new_prices < old_prices * MIN_PRICE_RATIO:
        raise ValueError(f"料金の件数が異常です (前回 {old_prices} → 今回 {new_prices})")


def commit(shops: dict[int, dict], missing: list[int] | None = None) -> None:
    """取得結果を検証して保存する。異常なら ValueError。"""
    now = now_epoch()
    previous = load_previous()
    merged = merge(previous, shops, missing or [], now)
    validate(merged, previous)
    save([merged[number] for number in sorted(merged)], now)


def main() -> int:
    parser = argparse.ArgumentParser(description="ジャンカラ料金表の自動更新")
    parser.add_argument("--start", type=int, default=1, help="開始番号 (既定: 1)")
    parser.add_argument("--end", type=int, default=2000, help="終了番号の上限 (既定: 2000)")
    parser.add_argument("--stop-after", type=int, default=60,
                        help="未使用番号がこの回数続いたら終了 (既定: 60)")
    parser.add_argument("--delay", type=float, default=1.0, help="リクエスト間隔の秒数 (既定: 1)")
    parser.add_argument("--dry-run", action="store_true", help="保存せず結果だけ表示")
    parser.add_argument("--cache-dir", type=Path, help="取得した HTML の保存先 (解析の調整用)")
    args = parser.parse_args()

    if args.delay < 0.5:
        parser.error("サイトへの負荷を避けるため --delay は 0.5 以上にしてください")
    if args.cache_dir:
        args.cache_dir.mkdir(parents=True, exist_ok=True)

    try:
        shops, failed, missing = sweep(args.start, args.end, args.stop_after, args.delay,
                                       args.cache_dir, lambda line: print(line, flush=True))
    except RateLimited:
        print("サイトから 429 が返ったため中断しました。保存していません。", file=sys.stderr)
        return 1

    print(f"\n店舗 {len(shops)} 件 / 取得失敗 {len(failed)} 件")
    if failed:
        print(f"取得失敗の番号: {failed}", file=sys.stderr)
    if args.dry_run:
        return 0
    try:
        commit(shops, missing)
    except ValueError as error:
        print(f"{error}。保存を中止しました。", file=sys.stderr)
        return 1
    print(f"保存しました: {DATA_DIR}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
