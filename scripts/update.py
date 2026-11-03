#!/usr/bin/env python3
"""Fetch public karaoke shop and tariff data. No application/server code required."""
from __future__ import annotations

import argparse
import html as html_lib
import json
import os
import re
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT
USER_AGENT = "karaoke-auto-getter/1.0 (public price data; GitHub Actions)"
DELAY = 0.20  # per-site gap; up to 5 requests/second
WORKERS = 8
JANKARA_URL = "https://jankara.ne.jp/shop/{}/"
BIGECHO_API = "https://shop.big-echo.jp/api/point"
MANEKI_URL = "https://www.karaokemanekineko.jp/locations/"
MANEKI_AJAX = "https://www.karaokemanekineko.jp/wpcore/wp-admin/admin-ajax.php"
MANEKI_PATTERN = re.compile(r"https://www\.karaokemanekineko\.jp/locations/[a-z0-9-]+/[a-z0-9-]+/[a-z0-9-]+-store/?")
JOYSOUND_SITEMAP = "https://shop.joysound.com/shop-sitemap.xml"
JOYSOUND_ENTRY = re.compile(r"<loc>(https://shop\.joysound\.com/shop/joysound-[^<]+)</loc>\s*<lastmod>([^<]+)</lastmod>")
COTE_BASE = "https://www.cotedazur.jp"
BANBAN_BASE = "https://karaoke-shin.jp"
BANBAN_LINK = re.compile(r'href="(https://karaoke-shin\.jp/shop-list/\d+\.html)">カラオケBanBan')
JOYJOY_BASE = "https://www.joyjoy.co.jp"
JOYJOY_LINK = re.compile(r"/store/view/([A-Za-z0-9_-]+)")
JOYJOY_POSTAL = {
    "長野県": ("38", "39"), "山梨県": ("40",), "静岡県": ("41", "42", "43"),
    "愛知県": ("44", "45", "46", "47", "48", "49"), "岐阜県": ("50",), "三重県": ("51",), "滋賀県": ("52",),
    "大阪府": ("53", "54", "55", "56", "57", "58", "59"), "福井県": ("91",), "石川県": ("92",), "富山県": ("93",),
}
CLUBDAM_LIST = "https://karaokeclub.jp/shop/"
CLUBDAM_LINK = re.compile(r'<h3><a href="(https://karaokeclub\.jp/shop/[^"#]+)"')
UTAHIROBA_BASE = "https://www.utahiro.com"
UTAHIROBA_LINK = re.compile(r'href="(/shops/st_[a-z0-9_-]+/\d+)"')
COTE_ENTRY = re.compile(
    r'<entry>\s*<name>([^<]+)</name>\s*<position lat="([\d.]+)" lng="([\d.]+)"\s*/>\s*'
    r"<address>([^<]*)</address>\s*<page>(/branch/detail/(\d+)\.html)</page>"
)
ROUND1_BASE = "https://www.round1.co.jp/shop"
ROUND1_AREAS = 7
ROUND1_LINK = re.compile(r'href="tenpo/([a-z0-9-]+)\.html')
ROUND1_ERROR = "ご指定のページは見つかりませんでした"
SPECIAL = re.compile(r"特別料金|年末年始|お盆|GW|ゴールデンウィーク|繁忙|特定日")
PREFECTURE = re.compile(r"^(北海道|東京都|大阪府|京都府|.{2,3}県)")
B_ECHO_HOSTS = {"big-echo.jp", "shop.big-echo.jp"}
# 店ごとの公式の予約ページ。まねきねこは公式アプリが入っていればアプリの予約画面が開く
RESERVE = {
    "まねきねこ": re.compile(r"https://reservation\.karaokemanekineko\.jp/\?storeId=\d+"),
    "カラオケ館": re.compile(r"https://liff\.line\.me/[\w-]+/book/oa/@[\w-]+/"),
    "ビッグエコー": re.compile(r"https://big-echo\.tottokun\.com/ownedmedia/ownedmedia_immediately_be/\w+"),
    "JOYSOUND": re.compile(r"https://reserve\.joysound\.com/reserve/shop_search\?prefecture_id=\d+(?:&(?:amp;)?location_cd=\d+)?"),
    "コート・ダジュール": re.compile(r"https://www\.hotpepper\.jp/strJ\d+/yoyaku/hpds/"),
    "BanBan": re.compile(r"https://res-reserve\.com/restaurants/banban\d+"),
    "CLUB DAM": re.compile(r"https://big-echo\.tottokun\.com/ownedmedia/ownedmedia_immediately_be/\w+"),
    "歌広場": re.compile(r"(?<=href=\")(?:https://www\.utahiro\.com)?/reserve/\?store_name=[^\"'<>\s]+"),
}
# ROUND1 は店を選ぶ前の共通の予約ページしかない
ROUND1_RESERVE = "https://www.round1.co.jp/yoyaku/"


class FetchStopped(RuntimeError):
    """Remote service rate-limited the crawler."""


def normalize(value):
    return " ".join(str(value or "").replace("\xa0", " ").split())


def numeric_price(tag):
    text = re.sub(r"[^0-9]", "", tag.get_text(" ", strip=True))
    return int(text) if text else None


def initial_text(tag):
    if tag is None:
        return ""
    return next((normalize(v) for v in tag.find_all(string=True, recursive=False) if v.strip()), "")


def read_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def publish(filename, shops, updated_at):
    DATA.mkdir(parents=True, exist_ok=True)
    destination = DATA / filename
    temporary = destination.with_suffix(".json.tmp")
    payload = {"schema_version": 1, "updated_at": updated_at, "shops": shops}
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(temporary, destination)


def http_session():
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


_thread_local = threading.local()


def worker_session():
    """Reuse HTTP/TLS connections per worker without sharing a Session."""
    if not hasattr(_thread_local, "session"):
        _thread_local.session = http_session()
    return _thread_local.session


class RequestPacer:
    """Space out requests globally for a source, even with multiple threads."""

    def __init__(self, interval):
        self.interval = max(0.125, interval)
        self.next_at = 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            due = max(time.monotonic(), self.next_at)
            self.next_at = due + self.interval
        duration = due - time.monotonic()
        if duration > 0:
            time.sleep(duration)


def request(session, url, *, redirect=True):
    # Raise on throttling; never interpret blocked pages as deleted stores.
    for attempt in range(3):
        try:
            response = session.get(url, timeout=35, allow_redirects=redirect)
            if response.status_code in (429, 503):
                raise FetchStopped(f"{response.status_code} {url}")
            if response.status_code in (500, 502, 504):
                raise requests.HTTPError(f"{response.status_code} {url}")
            # Some shop pages omit the charset; requests otherwise uses Latin-1.
            if not response.encoding or response.encoding.lower() == "iso-8859-1":
                response.encoding = "utf-8"
            return response
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(3 * (attempt + 1))
    raise AssertionError("unreachable")


def table_from_html(table):
    title = table.select_one("th.price_title")
    band = initial_text(title) or normalize(title.get_text(" ", strip=True).split("ドリンク")[0] if title else "")
    customers = [normalize(node.get_text(" ", strip=True)) for node in table.select("th.attribute")]
    plans = []
    active = None
    for row in table.select("tr"):
        if "price_name" in row.get("class", []):
            continue
        time_cell = row.select_one("td.price_time")
        if time_cell:
            notes = [normalize(p.get_text(" ", strip=True)) for p in time_cell.select("p")]
            active = {
                "name": initial_text(time_cell),
                "hours": next((s for s in notes if not s.startswith("最終受付")), ""),
                "last_entry": next((s.replace("最終受付", "", 1).strip() for s in notes if s.startswith("最終受付")), ""),
                "days": [],
            }
            plans.append(active)
        cells = row.select("td.price")
        if active is None or not cells:
            continue
        day = row.select_one("td.wd, td.hd")
        active["days"].append({
            "day": normalize(day.get_text(" ", strip=True)) if day else "全日",
            "prices": dict(zip(customers, (numeric_price(cell) for cell in cells))),
        })
    container = table.find_parent(class_="price_table_area")
    note = container.select_one("p.price_note") if container else None
    return {"band": band, "customers": customers, "plans": plans,
            "note": normalize(note.get_text(" ", strip=True)) if note else ""}


def super_jankara_table(section):
    header = section.select_one(".sjkprice__wrapper--header")
    customers = [normalize(node.get_text(" ", strip=True)) for node in header.select(".sjkprice__header")] if header else []
    title = section.select_one(".sjkprice__header--title")
    note = section.select_one("p.price_note")
    plans = []
    for course in section.select(".sjkprice__wrapper--course"):
        cell = course.select_one(".sjkprice__cell--course")
        prices_section = course.find_next_sibling(class_="sjkprice__wrapper--price")
        if not cell or not prices_section or not customers:
            continue
        details = " ".join(normalize(p.get_text(" ", strip=True)) for p in cell.select("p"))
        hours, _, last_entry = details.partition("最終受付")
        days = course.select(".sjkprice__cell--blue, .sjkprice__cell--pink")
        numbers = [numeric_price(el) for el in prices_section.select(".sjkprice__cell--price")]
        plans.append({
            "name": initial_text(cell), "hours": hours.strip(), "last_entry": last_entry.strip(),
            "days": [
                {"day": normalize(day.get_text(" ", strip=True)),
                 "prices": dict(zip(customers, numbers[index * len(customers):(index + 1) * len(customers)]))}
                for index, day in enumerate(days)
            ],
        })
    return {"band": normalize(title.get_text(" ", strip=True)).split(" ")[0] if title else "",
            "customers": customers, "plans": plans,
            "note": normalize(note.get_text(" ", strip=True)) if note else ""}


def parse_jankara(shop_id, markup):
    soup = BeautifulSoup(markup, "html.parser")
    heading = soup.select_one("h1.shopinfo__heading")
    address = soup.select_one("address.shopinfo__detail-address")
    clock = soup.select_one("i.fa-clock-o")
    hours = clock.find_next("p") if clock else None
    region, street, lat, lng = "", "", None, None
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            obj = json.loads(script.string or "")
        except (ValueError, TypeError):
            continue
        entries = obj if isinstance(obj, list) else [obj]
        for item in entries:
            if not isinstance(item, dict) or not isinstance(item.get("address"), dict):
                continue
            region = item["address"].get("addressRegion", "")
            street = item["address"].get("streetAddress", "")
            geo = item.get("geo") or {}
            lat, lng = geo.get("latitude"), geo.get("longitude")
            break
        if lat is not None:
            break

    regular, solo = [], []
    for table in soup.select("table.price_table"):
        (solo if table.find_parent(id="price-special") else regular).append(table_from_html(table))
    regular.extend(super_jankara_table(el) for el in soup.select("div.sjkprice"))
    price_section = soup.select_one("#price")
    alerts = []
    if price_section:
        for element in price_section.select("script,style,table"):
            element.decompose()
        alerts = sorted(set(SPECIAL.findall(price_section.get_text(" ", strip=True))))
    access_lines = [normalize(s) for s in address.stripped_strings] if address else []
    result = {
        "id": shop_id, "name": normalize(heading.get_text(" ", strip=True)) if heading else "",
        "url": JANKARA_URL.format(shop_id), "prefecture": region, "address": street,
        "access": access_lines[-1] if len(access_lines) >= 3 else "",
        "hours": normalize(hours.get_text(" ", strip=True)) if hours else "",
        "lat": lat, "lng": lng, "alerts": alerts,
        "regular": regular, "solo": solo,
    }
    if not result["name"] or lat is None or lng is None or not count_prices(result["regular"]):
        raise ValueError("Not a complete Jankara price page")
    return result


def count_prices(tables):
    return sum(price is not None for table in tables for plan in table["plans"]
               for day in plan["days"] for price in day["prices"].values())


def update_jankara(start=1, end=2000, stop_after=60, delay=DELAY):
    """Fetch in short parallel batches while preserving ordered missing-ID checks."""
    old_doc = read_json(DATA / "prices.json", {"updated_at": 0, "shops": []})
    old = {int(item["id"]): item for item in old_doc["shops"]}
    known_max = max(old, default=0)
    checked = int(time.time())
    fresh, missing, failures = {}, [], []
    miss_count = 0
    pacer = RequestPacer(delay)
    cancelled = threading.Event()

    def fetch_one(number):
        if cancelled.is_set():
            return number, None, "stopped", None
        pacer.wait()
        if cancelled.is_set():
            return number, None, "stopped", None
        try:
            resp = request(worker_session(), JANKARA_URL.format(number), redirect=False)
            if resp.status_code in (301, 302, 303, 307, 308, 404, 410):
                return number, None, "missing", None
            resp.raise_for_status()
            resp.encoding = "utf-8"
            return number, parse_jankara(number, resp.text), "found", None
        except FetchStopped as exc:
            cancelled.set()
            return number, None, "limited", exc
        except (requests.RequestException, ValueError) as exc:
            return number, None, "failed", exc

    finished = False
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for chunk_start in range(start, end + 1, 32):
            ids = range(chunk_start, min(end + 1, chunk_start + 32))
            # map preserves number ordering although page requests run concurrently.
            for number, shop, status, error in pool.map(fetch_one, ids):
                if status in ("stopped", "limited"):
                    cancelled.set()
                    raise FetchStopped(f"Jankara request limit at #{number}: {error}")
                if status == "missing":
                    missing.append(number)
                    miss_count += 1
                elif status == "found":
                    previous = old.get(number, {})
                    shop["checked_at"] = checked
                    shop["changed_at"] = (
                        checked if previous and any(shop[k] != previous.get(k) for k in ("regular", "solo"))
                        else previous.get("changed_at", 0)
                    )
                    shop["missing_strike"] = 0
                    shop["closed"] = False
                    fresh[number] = shop
                    miss_count = 0
                else:
                    failures.append(number)
                    miss_count = 0
                    print(f"Jankara #{number}: {error}", flush=True)
                if number % 50 == 0:
                    print(f"Jankara: checked {number}, found {len(fresh)}", flush=True)
                if miss_count >= stop_after and number > known_max:
                    finished = True
                    break
            if finished:
                break

    merged = dict(old)
    merged.update(fresh)
    for number in missing:
        if number in old:
            item = dict(merged[number])
            item["missing_strike"] = item.get("missing_strike", 0) + 1
            item["closed"] = item["missing_strike"] >= 3
            merged[number] = item
    active = [item for item in merged.values() if not item.get("closed")]
    old_active = [item for item in old.values() if not item.get("closed")]
    prices = sum(count_prices(x["regular"]) + count_prices(x["solo"]) for x in active)
    old_prices = sum(count_prices(x["regular"]) + count_prices(x["solo"]) for x in old_active)
    if not active or len(active) < 0.95 * len(old_active) or prices < 0.9 * old_prices:
        raise ValueError("Jankara validation failed: unexpected shop/price count decrease")
    publish("prices.json", [merged[n] for n in sorted(merged)],
            checked if not failures else old_doc["updated_at"])
    print(f"Jankara: {len(active)} active shops; failed={len(failures)}", flush=True)
    return not failures


def reserve_link(chain, text):
    """店のページなどから公式の予約ページを探す。なければ空。"""
    found = RESERVE[chain].search(text or "")
    return html_lib.unescape(found.group()) if found else ""


def shop_data(chain, ident, name, address, lat, lng, url, hours="", reserve=""):
    try:
        latitude, longitude = float(lat), float(lng)
    except (ValueError, TypeError):
        return None
    if not (20 <= latitude <= 46.5 and 122 <= longitude <= 154.5):
        return None
    address = unicodedata.normalize("NFKC", html_lib.unescape(unquote(address)))
    address = re.sub(r"〒?\d{3}-\d{4}", "", address)
    address = re.sub(r"(?<=\d)[−ー](?=\d)", "-", address)
    address = re.sub(r"<br\s*/?>", "", address, flags=re.I)
    address = re.sub(r"\s+", "", address)
    pref = PREFECTURE.match(address)
    return {
        "id": ident, "chain": chain, "name": normalize(html_lib.unescape(name)),
        "prefecture": pref.group() if pref else "", "address": address,
        "hours": normalize(hours), "lat": round(latitude, 6),
        "lng": round(longitude, 6), "url": url, "reserve": reserve,
    }


def be_reserve(session, key, old, pacer):
    """予約ページは店ごとの API にある。前回分を使い、まだ分からない店だけ聞く。"""
    if old and "reserve" in old:
        return old["reserve"]
    pacer.wait()
    response = request(session, f"{BIGECHO_API}/{key}")
    response.raise_for_status()
    return reserve_link("ビッグエコー", response.json().get("WEB予約への遷移", ""))


def be_shops(session, delay, previous=()):
    old = {shop["id"]: shop for shop in previous}
    pacer = RequestPacer(delay)
    time.sleep(delay)
    response = request(session, BIGECHO_API)
    response.raise_for_status()
    items = response.json()["items"]
    output = []
    for item in items:
        title = re.sub(r"^カラオケ\s*", "", item.get("name", ""))
        title = re.sub(r"^(ビッグエコー|BIG\s*ECHO)\s*", "", title, flags=re.I)
        ident = "be-" + str(item["key"])
        shop = shop_data("ビッグエコー", ident, title, item.get("address", ""),
                         item.get("latitude"), item.get("longitude"),
                         f"https://shop.big-echo.jp/map/{item['key']}")
        if shop:
            shop["reserve"] = be_reserve(session, item["key"], old.get(ident), pacer)
            output.append(shop)
    return output


def parse_kan(url, markup):
    title = re.search(r"<title>\s*([^|<]+)", markup)
    lat = re.search(r"!3d([0-9]{2}\.[0-9]+)", markup)
    lng = re.search(r"!2d(1[0-9]{2}\.[0-9]+)", markup)
    address = re.search(r'query=[^"+]*\+〒?[\d-]*\s*([^"]+?)"', markup)
    if not all((title, lat, lng, address)):
        return None
    return shop_data("カラオケ館", "kan-" + url.rsplit("/", 1)[-1],
                     title.group(1), address.group(1), lat.group(1), lng.group(1), url,
                     reserve=reserve_link("カラオケ館", markup))


def parse_maneki(url, markup):
    """Skip shops still being prepared; read pages without a map link from their structured data."""
    if re.search(r"businessHoursRow__time\">\s*準備中", markup):
        return None
    title = re.search(r"<title>\s*([^｜|<]+)", markup)
    coords = (re.search(r"ll=([0-9]{2}\.[0-9]+),(1[0-9]{2}\.[0-9]+)", markup)
              or re.search(r'"latitude":\s*([0-9]{2}\.[0-9]+),\s*"longitude":\s*(1[0-9]{2}\.[0-9]+)', markup))
    if not title or not coords:
        raise ValueError("Shop page format changed")
    name = normalize(title.group(1))
    query = re.search(r'''maps[^"']*?[?&;]q=([^"'&]+)''', markup)
    if query:
        address = unquote(query.group(1)).replace("+", " ")
        address = re.sub(rf"^.*?{re.escape(name)}\s*", "", address)
    else:
        listed = re.search(r"住所</dt>\s*<dd>\s*(?:<span>〒[^<]*</span>\s*<br>\s*)?<span>([^<]+)</span>", markup)
        address = normalize(listed.group(1)) if listed else ""
    hours = re.search(r"営業時間</[^>]+>\s*(?:<[^>]+>\s*)*((?:\d|24時間)[^<]{2,40})", markup)
    return shop_data("まねきねこ", "mn-" + url.rstrip("/").split("/")[-1], name,
                     address, coords.group(1), coords.group(2), url,
                     hours.group(1) if hours else "", reserve_link("まねきねこ", markup))


def maneki_urls(session, delay):
    time.sleep(delay)
    html = request(session, MANEKI_URL).text
    found = re.search(r"LOC_AJAX\s*=\s*(\{.*?\})", html)
    if not found:
        raise ValueError("Manekineko pagination token missing")
    nonce = json.loads(found.group(1))["nonce"]
    urls = dict.fromkeys(MANEKI_PATTERN.findall(html))
    page = 1
    for _ in range(200):  # bound pagination on a broken API
        time.sleep(delay)
        response = session.post(MANEKI_AJAX, data={
            "action": "loc_load_more", "nonce": nonce, "page": page,
            "paged": page, "per_page": 30, "pref": "", "city": "", "q": "",
        }, timeout=35)
        if response.status_code in (429, 503):
            raise FetchStopped(f"Manekineko HTTP {response.status_code}")
        response.raise_for_status()
        data = response.json()["data"]
        urls.update(dict.fromkeys(MANEKI_PATTERN.findall(data["html"])))
        if not data.get("has_more"):
            return list(urls)
        next_page = int(data.get("next_page", page + 1))
        if next_page <= page:
            raise ValueError("Manekineko pagination did not advance")
        page = next_page
    raise ValueError("Manekineko exceeded pagination limit")


def details(urls, parser, previous, session, delay, *, allow_unlisted=False, pacer=None):
    """Skip known shop pages, fetch only new shops concurrently, preserve order."""
    urls = list(dict.fromkeys(urls))
    # 予約ページを読む前に保存した店は、1 回だけ読み直す
    known = {item["url"]: item for item in previous if "reserve" in item}
    unknown = [url for url in urls if url not in known]
    found = {}
    failures = []
    unavailable = []
    unlisted = []
    cancelled = threading.Event()
    pacer = pacer or RequestPacer(delay)

    def fetch_one(url):
        if cancelled.is_set():
            return url, None, "stopped"
        pacer.wait()
        if cancelled.is_set():
            return url, None, "stopped"
        try:
            resp = request(worker_session(), url)
            # The site's sitemap can contain retired stores. A genuine 404/410
            # is not a parser/network failure; only skip this individual URL.
            if resp.status_code in (404, 410):
                return url, None, "unavailable"
            resp.raise_for_status()
            result = parser(url, resp.text)
            if result is None:
                if allow_unlisted:
                    return url, None, "unlisted"
                raise ValueError("Shop page format changed")
            return url, result, ""
        except FetchStopped as exc:
            cancelled.set()
            return url, None, f"limited: {exc}"
        except (requests.RequestException, ValueError) as exc:
            return url, None, str(exc)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for index, (url, shop, error) in enumerate(pool.map(fetch_one, unknown), 1):
            if error == "unlisted":
                unlisted.append(url)
            elif error == "unavailable":
                unavailable.append(url)
                print(f"Shop no longer available (404/410): {url}", flush=True)
            elif error:
                failures.append(url)
                print(f"Shop detail failed ({url}): {error}", flush=True)
            else:
                found[url] = shop
            if index % 50 == 0:
                print(f"New shop details: {index}/{len(unknown)}", flush=True)
    if cancelled.is_set():
        raise FetchStopped("Shop detail requests were rate-limited")
    if failures:
        raise ValueError(f"{len(failures)} shop details failed; keeping previous chain data")
    # Fail closed if too many URLs disappeared: probable broken sitemap or blocking.
    if len(unavailable) > max(5, int(len(unknown) * 0.05)):
        raise ValueError(f"Too many unavailable shop pages: {len(unavailable)}/{len(unknown)}")
    output = [known[url] if url in known else found[url] for url in urls
              if url not in unavailable and url not in unlisted]
    minimum = 0.6 if allow_unlisted else 0.8
    if urls and len(output) < len(urls) * minimum:
        raise ValueError(f"Shop listing unexpectedly shrank: {len(urls)} -> {len(output)}")
    return output



def parse_joysound(url, markup):
    """Exclude closed stores without geo or stores renovated into other brands."""
    title = re.search(r"<title>\s*JOYSOUND([^|<]+)", markup)
    geo = re.search(r'"latitude":\s*"([\d.]+)",\s*"longitude":\s*"([\d.]+)"', markup)
    region = re.search(r'"addressRegion":\s*"([^"]*)"', markup)
    locality = re.search(r'"addressLocality":\s*"([^"]*)"', markup)
    street = re.search(r'"streetAddress":\s*"([^"]*)"', markup)
    if not all((title, geo, region, locality, street)) or "リニューアル" in title.group(1):
        return None
    slug = url.rstrip("/").rsplit("/", 1)[1]
    return shop_data("JOYSOUND", f"js-{slug}", title.group(1),
                     region.group(1) + locality.group(1) + street.group(1),
                     geo.group(1), geo.group(2), url, reserve=reserve_link("JOYSOUND", markup))


def joysound_shops(session, previous, delay):
    """Recheck newly listed or modified shops, retaining unchanged entries."""
    resp = request(session, JOYSOUND_SITEMAP)
    resp.raise_for_status()
    modified = dict(JOYSOUND_ENTRY.findall(resp.text))
    if not modified:
        raise ValueError("JOYSOUND sitemap returned no shop URLs")
    unchanged = [s for s in previous if modified.get(s["url"]) == s.get("modified")]
    shops = details(list(modified), parse_joysound, unchanged, session, delay,
                    allow_unlisted=True)
    return [{**shop, "modified": modified[shop["url"]]} for shop in shops]


def parse_cotedazur(markup):
    shops = []
    for name, lat, lng, address, page, number in COTE_ENTRY.findall(markup):
        item = shop_data("コート・ダジュール", f"cd-{number}", name, address,
                         lat, lng, COTE_BASE + page)
        if item:
            shops.append(item)
    return shops


def cotedazur_shops(session, delay, previous=()):
    """一覧の XML に予約ページがないので、まだ分からない店だけ店のページを読む。"""
    resp = request(session, f"{COTE_BASE}/branch/data/branch/shopinfo.xml")
    resp.raise_for_status()
    shops = parse_cotedazur(resp.text)
    if not shops:
        raise ValueError("Cote d'Azur shop XML contains no valid entries")
    old = {shop["id"]: shop for shop in previous}
    pacer = RequestPacer(delay)
    for shop in shops:
        if "reserve" in old.get(shop["id"], {}):
            shop["reserve"] = old[shop["id"]]["reserve"]
            continue
        pacer.wait()
        page = request(session, shop["url"])
        if page.status_code in (404, 410):
            continue
        page.raise_for_status()
        shop["reserve"] = reserve_link("コート・ダジュール", page.text)
    return shops


def round1_has_karaoke(markup):
    """Ignore karaoke links outside the 'facilities available' section."""
    without_scripts = re.sub(r"(?is)<!--.*?-->|<(script|style).*?</\1>", " ", markup)
    without_scripts = re.sub(r'<img[^>]*alt="([^"]*)"[^>]*>', r" \1 ", without_scripts)
    content = re.sub(r"<[^>]+>", " ", without_scripts)
    section = re.search(
        r"店舗で遊べる施設(?!.*店舗で遊べる施設)(.*?)(?:お知らせ|Posts by)",
        content, re.S
    )
    return bool(section) and "カラオケ" in section.group(1)


def parse_round1(url, markup, facility_markup):
    if ROUND1_ERROR in markup or not round1_has_karaoke(facility_markup):
        return None
    title = re.search(
        r"<title>(?:【ラウンドワン】|ラウンドワン\s*\|\s*)?(?:ラウンドワン\s*)?([^｜|<]+)",
        markup,
    )
    coords = re.search(r"(\d{2}\.\d{4,})\s*,\s*(1\d{2}\.\d{4,})", markup)
    address = re.search(r"〒\d{3}[-－]\d{4}\s*(?:<br\s*/?>)?\s*([^<&]+)", markup)
    if not all((title, coords, address)):
        return None
    slug = url.rsplit("/", 1)[1].removesuffix(".html")
    return shop_data("ROUND1", f"r1-{slug}", title.group(1),
                     address.group(1), coords.group(1), coords.group(2), url, reserve=ROUND1_RESERVE)


def round1_shops(session, previous, delay):
    """Discover new facilities and check the known ones for actual closure."""
    pacer = RequestPacer(delay)
    slugs = {}
    for area in range(1, ROUND1_AREAS + 1):
        pacer.wait()
        resp = request(session, f"{ROUND1_BASE}/area0{area}.html")
        resp.raise_for_status()
        slugs.update(dict.fromkeys(ROUND1_LINK.findall(resp.text)))
    if not slugs:
        raise ValueError("ROUND1 area pages contained no store links")
    urls = [f"{ROUND1_BASE}/tenpo/{slug}.html" for slug in slugs]
    listed = set(urls)
    known = [{**shop, "reserve": ROUND1_RESERVE} for shop in previous if shop["url"] in listed]

    def is_live(shop):
        pacer.wait()
        try:
            resp = worker_session().head(shop["url"], timeout=25, allow_redirects=False)
            if resp.status_code in (429, 503):
                raise FetchStopped(f"ROUND1 throttled: HTTP {resp.status_code}")
            return shop, resp.status_code not in (301, 302, 404, 410)
        except requests.RequestException as exc:
            # A timeout or 5xx cannot be used as evidence for a closure.
            print(f"ROUND1 HEAD inconclusive {shop['id']}: {exc}", flush=True)
            return shop, True

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        retained = [shop for shop, live in pool.map(is_live, known) if live]

    def parse_new(url, markup):
        if ROUND1_ERROR in markup:
            return None
        pacer.wait()
        resp = request(worker_session(), url.replace("/tenpo/", "/facility/"))
        if resp.status_code in (404, 410):
            return None
        resp.raise_for_status()
        return parse_round1(url, markup, resp.text)

    return details(urls, parse_new, retained, session, delay,
                   allow_unlisted=True, pacer=pacer)


def parse_banban(url, markup):
    name = re.search(r"<title>\s*カラオケBanBan\s*([^|<]+)", markup)
    coords = re.search(r"center=(\d{2}\.\d+),(1\d{2}\.\d+)", markup)
    address = re.search(r"〒\d{3}-\d{4}\s*(?:<br\s*/?>)?\s*([^<]+)", markup)
    hours = re.search(r"営業時間[:：]\s*(?:<[^>]+>\s*)*([^<]{3,40})", markup)
    if not all((name, coords, address)):
        return None
    number = url.rsplit("/", 1)[1].removesuffix(".html")
    return shop_data("BanBan", f"bb-{number}", name.group(1), address.group(1),
                     coords.group(1), coords.group(2), url, hours.group(1) if hours else "",
                     reserve_link("BanBan", markup))


def banban_shops(session, previous, delay):
    """The area pages also list sister brands such as カラオケALL; keep BanBan only."""
    pacer = RequestPacer(delay)
    urls = []
    for area in range(1, 48):
        pacer.wait()
        resp = request(session, f"{BANBAN_BASE}/shop-area/{area:02d}.html")
        if resp.status_code == 404:
            continue
        resp.raise_for_status()
        urls.extend(BANBAN_LINK.findall(resp.text))
    if not urls:
        raise ValueError("BanBan area pages contained no store links")
    # A few pages have no map; those shops cannot be placed and are left out
    return details(urls, parse_banban, previous, session, delay, allow_unlisted=True, pacer=pacer)


def joyjoy_prefecture(postal):
    for prefecture, prefixes in JOYJOY_POSTAL.items():
        if postal[:2] in prefixes:
            return prefecture
    return None


def parse_joyjoy(url, markup):
    """Some addresses omit the prefecture, so it comes from the postal code."""
    name = re.search(r"<h2[^>]*>\s*([^<]+?)\s*<span>", markup)
    lat = re.search(r"!3d(\d{2}\.\d+)", markup)
    lng = re.search(r"!2d(1\d{2}\.\d+)", markup)
    address = re.search(r"ADDRESS.*?<td>\s*(?:（新）\s*)?(?:〒?(\d{3})-\d{4}\s*)?([^<]+)", markup, re.S)
    if not all((name, lat, lng, address)):
        return None
    # Moved addresses read "（新）〒... new （旧）〒... old"
    text = normalize(address.group(2).split("（旧）")[0])
    if not PREFECTURE.match(text):
        prefecture = "愛知県" if text.startswith("名古屋市") else joyjoy_prefecture(address.group(1) or "")
        if not prefecture:
            return None
        text = prefecture + text
    slug = url.rsplit("/", 1)[1]
    return shop_data("JOYJOY", f"jj-{slug}", name.group(1), text, lat.group(1), lng.group(1), url)


def joyjoy_shops(session, previous, delay):
    pacer = RequestPacer(delay)
    pacer.wait()
    top = request(session, f"{JOYJOY_BASE}/")
    top.raise_for_status()
    slugs = {}
    for area in dict.fromkeys(re.findall(r'href="/store/index/(\d+)"', top.text)):
        pacer.wait()
        resp = request(session, f"{JOYJOY_BASE}/store/index/{area}")
        resp.raise_for_status()
        slugs.update(dict.fromkeys(JOYJOY_LINK.findall(resp.text)))
    if not slugs:
        raise ValueError("JOYJOY area pages contained no store links")
    shops = details([f"{JOYJOY_BASE}/store/view/{slug}" for slug in slugs], parse_joyjoy,
                    previous, session, delay, allow_unlisted=True, pacer=pacer)
    # The same page is linked under more than one spelling of its slug
    return list({shop["address"]: shop for shop in reversed(shops)}.values())[::-1]


def parse_clubdam(url, markup):
    title = re.search(r"<title>[^<]*?&nbsp;\s*([^<]+)", markup)
    coords = re.search(r"[?&;]ll=(\d{2}\.\d+),(1\d{2}\.\d+)", markup)
    latlng = coords.groups() if coords else None
    embed = re.search(r"!2d(1\d{2}\.\d+)!3d(\d{2}\.\d+)", markup)
    if embed and not latlng:
        latlng = embed.group(2), embed.group(1)
    address = re.search(r"住所：</dt>\s*<dd>\s*(?:〒[\d-]+)?(?:&nbsp;)?\s*([^<]+)", markup)
    if not all((title, latlng, address)):
        return None
    name = unicodedata.normalize("NFKC", html_lib.unescape(title.group(1)))
    name = re.sub(r"^カラオケ\s*CLUB\s*DAM\s*", "", name)
    name = re.sub(r"\s*(?:\(セルフカラオケ\)|【[^】]*】)", "", name).strip()
    return shop_data("CLUB DAM", f"dam-{name}", name, address.group(1),
                     latlng[0], latlng[1], url, reserve=reserve_link("CLUB DAM", markup))


def clubdam_shops(session, previous, delay):
    time.sleep(delay)
    resp = request(session, CLUBDAM_LIST)
    resp.raise_for_status()
    urls = CLUBDAM_LINK.findall(resp.text)
    if not urls:
        raise ValueError("CLUB DAM shop list contained no store links")
    return details(urls, parse_clubdam, previous, session, delay)


def parse_utahiroba(url, markup):
    # 予約フォームへのリンクは相対パスで、店名が日本語のまま書かれている
    reserve = reserve_link("歌広場", markup)
    name = re.search(r"<title>\s*([^｜|<]+)", markup)
    lat = re.search(r"!3d(\d{2}\.\d+)", markup)
    lng = re.search(r"!2d(1\d{2}\.\d+)", markup)
    address = re.search(r">住所</div>\s*</div>\s*<div[^>]*>\s*<div[^>]*>\s*([^<]+)", markup)
    hours = re.search(r">営業時間</div>\s*</div>\s*<div[^>]*>\s*<div[^>]*>\s*([^<]{3,60})", markup)
    if not all((name, lat, lng, address)):
        return None
    return shop_data("歌広場", "uh-" + url.rsplit("/", 1)[1], name.group(1), address.group(1),
                     lat.group(1), lng.group(1), url, hours.group(1) if hours else "",
                     urljoin(UTAHIROBA_BASE, quote(reserve, safe=":/?=&%")) if reserve else "")


def utahiroba_shops(session, previous, delay):
    pacer = RequestPacer(delay)
    paths = {}
    for page in range(1, 40):  # bound pagination on a broken site
        pacer.wait()
        resp = request(session, f"{UTAHIROBA_BASE}/shops/search?ible-categoryselectbox1=&page={page}")
        if resp.status_code == 404 and paths:  # past the last page
            break
        resp.raise_for_status()
        found = dict.fromkeys(UTAHIROBA_LINK.findall(resp.text))
        if not found.keys() - paths.keys():
            break
        paths.update(found)
    if not paths:
        raise ValueError("Utahiroba search pages contained no store links")
    return details([UTAHIROBA_BASE + path for path in paths], parse_utahiroba, previous,
                   session, delay, pacer=pacer)


def update_chains(delay=DELAY):
    old_doc = read_json(DATA / "chains.json", {"updated_at": 0, "shops": []})
    old = old_doc["shops"]
    session = http_session()
    results = []
    failures = []
    chains = ("ビッグエコー", "カラオケ館", "まねきねこ",
              "JOYSOUND", "コート・ダジュール", "ROUND1",
              "BanBan", "JOYJOY", "CLUB DAM", "歌広場")
    for chain in chains:
        previous = [s for s in old if s["chain"] == chain]
        try:
            if chain == "ビッグエコー":
                current = be_shops(session, delay, previous)
            elif chain == "カラオケ館":
                time.sleep(delay)
                xml = request(session, "https://karaokekan.jp/sitemap.xml")
                xml.raise_for_status()
                urls = re.findall(r"<loc>(https://karaokekan\.jp/shop/detail/\d+)</loc>", xml.text)
                if not urls:
                    raise ValueError("Karaoke-kan sitemap contains no store URLs")
                current = details(urls, parse_kan, previous, session, delay)
            elif chain == "まねきねこ":
                current = details(maneki_urls(session, delay), parse_maneki, previous, session, delay,
                                  allow_unlisted=True)
            elif chain == "JOYSOUND":
                current = joysound_shops(session, previous, delay)
            elif chain == "コート・ダジュール":
                current = cotedazur_shops(session, delay, previous)
            elif chain == "ROUND1":
                current = round1_shops(session, previous, delay)
            elif chain == "BanBan":
                current = banban_shops(session, previous, delay)
            elif chain == "JOYJOY":
                current = joyjoy_shops(session, previous, delay)
            elif chain == "CLUB DAM":
                current = clubdam_shops(session, previous, delay)
            elif chain == "歌広場":
                current = utahiroba_shops(session, previous, delay)
            else:
                raise AssertionError(f"Unsupported chain {chain}")
            if not current or len(current) < 0.8 * len(previous):
                raise ValueError(f"Shop count decreased: {len(previous)} -> {len(current)}")
        except (FetchStopped, requests.RequestException, ValueError, KeyError, TypeError, AttributeError) as exc:
            failures.append(chain)
            current = previous
            print(f"{chain}: failed ({exc}); old data retained", flush=True)
        print(f"{chain}: {len(current)} shops", flush=True)
        results.extend(current)
    if not results:
        raise ValueError("No chain shops available")
    publish("chains.json", results, old_doc["updated_at"] if failures else int(time.time()))
    return not failures


def be_page_url(session, ident, old, delay, limiter=None):
    if old and old.get("page"):
        candidate = old["page"]
    else:
        if limiter:
            limiter.wait()
        else:
            time.sleep(delay)
        response = request(session, BIGECHO_API + "/" + ident.removeprefix("be-"))
        response.raise_for_status()
        candidate = response.json()["短縮ＵＲＬ"]
    parsed = urlparse(candidate)
    if parsed.scheme != "https" or parsed.hostname not in B_ECHO_HOSTS:
        raise ValueError(f"Disallowed Big Echo URL: {candidate}")
    return candidate


def parse_be_price(markup):
    soup = BeautifulSoup(markup, "html.parser")
    main = soup.select_one("#price")
    if main is None:
        return None
    sections = []
    heading, time_band = "", ""
    for item in main.select("h3,h4,table"):
        if item.name == "h3":
            heading, time_band = normalize(item.get_text(" ", strip=True)), ""
        elif item.name == "h4":
            time_band = normalize(item.get_text(" ", strip=True))
        else:
            rows = [[normalize(cell.get_text(" ", strip=True)) for cell in tr.select("th,td")]
                    for tr in item.select("tr")]
            data_rows = [row for row in rows if len(row) > 1]
            if not data_rows:
                continue
            singles = [r[0] for r in rows if len(r) == 1 and r[0]]
            label = singles[0] if singles else ""
            normalized = unicodedata.normalize("NFKC", heading + time_band + label).replace(" ", "")
            if "30分" not in normalized:
                continue
            sections.append({
                "heading": heading, "time": time_band, "title": label,
                "note": " ".join(singles[1:]), "header": data_rows[0], "rows": data_rows[1:],
            })
    dt = next((node for node in soup.select("dt") if "営業時間" in node.get_text(" ", strip=True)), None)
    hours = dt.find_next_sibling("dd") if dt else None
    return {"hours": normalize(hours.get_text(" ", strip=True)) if hours else "", "sections": sections}


def update_be_prices(delay=DELAY):
    chains = read_json(DATA / "chains.json", {"shops": []})["shops"]
    ids = sorted(s["id"] for s in chains if s["chain"] == "ビッグエコー")
    if not ids:
        raise ValueError("Big Echo shops are missing from chains.json")
    old_doc = read_json(DATA / "chain_prices.json", {"updated_at": 0, "shops": {}})
    previous = old_doc["shops"]
    selected = {key: previous[key] for key in ids if key in previous}
    due = ids  # Refresh each Big Echo shop on every daily run.
    throttle = threading.Event()
    limiter = RequestPacer(delay)
    errors = []
    def worker(key):
        if throttle.is_set():
            return key, None, "stopped"
        session = worker_session()
        for attempt in range(2):
            try:
                target = be_page_url(session, key, selected.get(key), delay, limiter=limiter)
                limiter.wait()
                if throttle.is_set():
                    return key, None, "stopped"
                response = request(session, target)
                response.raise_for_status()
                parsed = parse_be_price(response.text)
                if parsed is None:
                    return key, None, "no price section"
                landing = response.url.replace("http://", "https://", 1)
                host = urlparse(landing).hostname
                if host not in B_ECHO_HOSTS:
                    landing = target
                return key, {**parsed, "page": landing, "checked_at": int(time.time())}, ""
            except FetchStopped as exc:
                throttle.set()
                return key, None, str(exc)
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                error = str(exc)
                if attempt == 0:
                    time.sleep(2)
        return key, None, error

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        tasks = {pool.submit(worker, key): key for key in due}
        for future in as_completed(tasks):
            key, data, error = future.result()
            if data is None:
                errors.append(key)
                print(f"Big Echo {key}: {error}", flush=True)
            else:
                selected[key] = data
    previous_tables = sum(bool(s.get("sections")) for s in previous.values())
    current_tables = sum(bool(s.get("sections")) for s in selected.values())
    if current_tables == 0 or current_tables < 0.8 * previous_tables:
        raise ValueError(f"Big Echo table count unexpectedly decreased: {previous_tables} -> {current_tables}")
    publish("chain_prices.json", dict(sorted(selected.items())),
            old_doc["updated_at"] if errors else int(time.time()))
    print(f"Big Echo: {current_tables}/{len(ids)} with 30min tables; failed={len(errors)}", flush=True)
    return not errors


def main():
    argp = argparse.ArgumentParser(description="Update karaoke price JSON files from public shop pages")
    argp.add_argument("target", choices=("all", "jankara", "chains", "bigecho"))
    argp.add_argument("--delay", type=float, default=DELAY, help="Minimum per-source request spacing; default 0.20s")
    args = argp.parse_args()
    if args.delay < 0.125:
        argp.error("--delay must be at least 0.125 seconds to avoid excessive request volume")
    actions = {
        "jankara": lambda: update_jankara(delay=args.delay),
        "chains": lambda: update_chains(delay=args.delay),
        "bigecho": lambda: update_be_prices(delay=args.delay),
    }
    all_ok = True
    if args.target == "all":
        # Independent site groups run simultaneously; Big Echo price updates
        # wait only for the chain directory, not the entire Jankara sweep.
        with ThreadPoolExecutor(max_workers=3) as pool:
            chain_future = pool.submit(update_chains, delay=args.delay)
            jankara_future = pool.submit(update_jankara, delay=args.delay)

            def update_be_after_chains():
                chain_ok = chain_future.result()
                return update_be_prices(delay=args.delay)

            be_future = pool.submit(update_be_after_chains)
            futures = {
                "jankara": jankara_future,
                "chains": chain_future,
                "bigecho": be_future,
            }
            for name, future in futures.items():
                try:
                    if not future.result():
                        all_ok = False
                except (FetchStopped, requests.RequestException, ValueError, KeyError, TypeError) as exc:
                    all_ok = False
                    print(f"{name} aborted: {exc}", file=sys.stderr, flush=True)
    else:
        try:
            all_ok = actions[args.target]()
        except (FetchStopped, requests.RequestException, ValueError, KeyError, TypeError) as exc:
            all_ok = False
            print(f"{args.target} aborted: {exc}", file=sys.stderr, flush=True)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
