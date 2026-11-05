#!/usr/bin/env python3
"""公式 JOYSOUND 店舗ページから標準料金表を取得する。"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

import update

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "source" / "joysound_prices.json"
USER_AGENT = update.USER_AGENT
SCHEMA_VERSION = 1
DELAY = 0.5
PARALLEL = 4
ATTEMPTS = 2
THROTTLED_CODES = (429, 503)
RECHECK_SECONDS = 20 * 3600
MIN_SHOP_RATIO = 0.8
SCOPE = "standard-rates-v1"
PRICE_PATTERN = re.compile(r"([\d,]+)\s*円")
NO_PRICE = {"", "-", "−", "ー", "―", "—"}


def clean(text: str | None) -> str:
    return " ".join(unicodedata.normalize("NFKC", text or "").split())


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
    compact = text.replace(" ", "")
    if compact in NO_PRICE:
        return None

    tax_cell = cell.select_one(".pt")
    if tax_cell is not None:
        text = clean(tax_cell.get_text(" ", strip=True))
        compact = text.replace(" ", "")
    if any(mark in compact for mark in ("税別", "税抜")):
        raise ValueError(f"Tax-exclusive amount in rate cell: {text!r}")

    # The current site renders the tax-included amount in .pt. Older pages may
    # have the same value as the only amount in the cell, so accept one amount.
    matches = PRICE_PATTERN.findall(compact)
    if len(matches) == 1:
        return int(matches[0].replace(",", ""))
    if "税込" in compact:
        after_tax = compact.split("税込", 1)[1]
        match = PRICE_PATTERN.search(after_tax)
        if match:
            return int(match[1].replace(",", ""))
    raise ValueError(f"Could not identify one tax-included amount: {text!r}")


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


def parse_page(html: str, page: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    sections = []
    for heading in soup.find_all("h2"):
        title = clean(heading.get_text(" ", strip=True))
        if title not in ("昼の料金", "夜の料金"):
            continue
        period = "昼" if "昼" in title else "夜" if "夜" in title else title
        contents = heading.find_next_sibling("div", class_="charges-special__contents")
        if contents is None:
            raise ValueError(f"Rate section has no content: {title}")
        for table in contents.select(".jt"):
            header = table.select_one(".jr-h")
            if header is None:
                raise ValueError(f"Rate table has no column header: {title}")
            cells = header.find_all(class_="jc", recursive=False)
            if len(cells) < 3 or "jc-k" not in cells[0].get("class", []):
                raise ValueError(f"Unexpected rate table header: {title}")
            day = clean(cells[0].get_text(" ", strip=True))
            customers = [class_for_header(cell.get_text(" ", strip=True)) for cell in cells[1:]]
            if not day or any(value is None for value in customers) or len(set(customers)) != len(customers):
                raise ValueError(f"Unrecognized rate columns: {title} / {day} / {customers}")

            rows = []
            for row in table.select(".jr:not(.jr-h)"):
                row_cells = row.find_all(class_="jc", recursive=False)
                if len(row_cells) != len(customers) + 1 or "jc-k" not in row_cells[0].get("class", []):
                    raise ValueError(f"Unexpected rate row layout: {title} / {day}")
                label = clean(row_cells[0].get_text(" ", strip=True))
                prices = {customer: tax_included_price(cell) for customer, cell in zip(customers, row_cells[1:])}
                drinks = {customer: drink_tag(cell) for customer, cell in zip(customers, row_cells[1:])}
                if any(value is not None and not 1 <= value <= 30000 for value in prices.values()):
                    raise ValueError(f"Unreasonable rate in {title} / {day}: {label}")
                if any(value is not None for value in prices.values()):
                    rows.append({"label": label, "prices": prices, "drinks": drinks})
            if not rows:
                raise ValueError(f"Rate table has no amounts: {title} / {day}")
            sections.append({"period": period, "day": day, "rows": rows})

    if not sections or not any("30分" in row["label"] for section in sections for row in section["rows"]):
        raise ValueError("No standard 30-minute rate tables found")
    return {"page": page, "scope": SCOPE, "sections": sections,
            "opening_hours": official_simple_hours(soup), "checked_at": int(time.time())}


def load_json(path: Path, default: dict) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save(shops: dict[str, dict], updated_at: int) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"schema_version": SCHEMA_VERSION, "updated_at": updated_at, "shops": shops},
                         ensure_ascii=False, separators=(",", ":"))
    temp = DATA_FILE.with_suffix(".json.tmp")
    temp.write_text(payload + "\n", encoding="utf-8", newline="")
    os.replace(temp, DATA_FILE)


class Throttled(RuntimeError):
    pass


class Pacer:
    def __init__(self, interval: float):
        self.interval = interval
        self.next_at = 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            due = max(time.monotonic(), self.next_at)
            self.next_at = due + self.interval
        delay = due - time.monotonic()
        if delay > 0:
            time.sleep(delay)


_thread_local = threading.local()


def worker_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        _thread_local.session = session
    return _thread_local.session


def fetch_page(page: str, pacer: Pacer, stopped: threading.Event) -> dict:
    parts = urlsplit(page)
    if parts.scheme != "https" or parts.hostname != "shop.joysound.com" or not parts.path.startswith("/shop/"):
        raise ValueError(f"Not an official JOYSOUND shop URL: {page}")
    error = ""
    for attempt in range(ATTEMPTS):
        if stopped.is_set():
            raise Throttled("stopped after site throttling")
        pacer.wait()
        try:
            response = worker_session().get(page, timeout=30)
            if response.status_code in THROTTLED_CODES:
                stopped.set()
                raise Throttled(f"HTTP {response.status_code}")
            response.raise_for_status()
            response.encoding = "utf-8"
            result = parse_page(response.text, page)
            result["checked_at"] = int(time.time())
            return result
        except Throttled:
            raise
        except (requests.RequestException, ValueError) as exc:
            error = str(exc)
            if attempt + 1 < ATTEMPTS:
                time.sleep(2)
    raise ValueError(error)


def table_count(shops: dict[str, dict]) -> int:
    return sum(bool(shop.get("sections")) for shop in shops.values())


def update(log=print) -> bool:
    chain_data = load_json(ROOT / "chains.json", {"shops": []})
    listed = {shop["id"]: shop["url"] for shop in chain_data["shops"]
              if shop.get("chain") == "JOYSOUND"}
    if not listed:
        raise ValueError("JOYSOUND shops are missing from chains.json")

    old_doc = load_json(DATA_FILE, {"updated_at": 0, "shops": {}})
    previous = old_doc.get("shops", {})
    now = int(time.time())
    selected = {key: previous[key] for key in listed if key in previous}
    due = [key for key in listed if now - selected.get(key, {}).get("checked_at", 0) >= RECHECK_SECONDS
           or selected.get(key, {}).get("scope") != SCOPE
           or selected.get(key, {}).get("page") != listed[key]]
    pacer = Pacer(DELAY)
    stopped = threading.Event()
    errors = []
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        futures = {pool.submit(fetch_page, listed[key], pacer, stopped): key for key in due}
        for future in as_completed(futures):
            key = futures[future]
            try:
                selected[key] = future.result()
            except (requests.RequestException, ValueError, Throttled) as exc:
                errors.append(key)
                log(f"JOYSOUND {key}: {exc}")

    selected = {key: selected[key] for key in listed if key in selected}
    found = table_count(selected)
    minimum = max(1, int(len(listed) * MIN_SHOP_RATIO + 0.999))
    if found < minimum:
        raise ValueError(f"Too few JOYSOUND price pages parsed: {found}/{len(listed)} (minimum {minimum})")
    save(selected, old_doc.get("updated_at", 0) if errors else now)
    log(f"JOYSOUND: {found}/{len(listed)} 店、表を取得 (失敗 {len(errors)})")
    return not errors


if __name__ == "__main__":
    try:
        success = update(log=lambda line: print(line, flush=True))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"JOYSOUND price update failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(0 if success else 1)
