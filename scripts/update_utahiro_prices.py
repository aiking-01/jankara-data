#!/usr/bin/env python3
"""歌広場の店のページから、料金表・営業時間・告知の文字を取り出して source/utahiro_prices.json に保存する。

金額への変換は scripts/web/utahiro.js が行う。ここではページの文字と表の並びをそのまま残し、意味は読まない。

- 店は chains.json の歌広場の店（update.py が週 1 回作る）。ページは www.utahiro.com/shops/st_xxx/N。
- 公式サイトは ETag も gzip も返さない（1 ページ約 70KB）。店ごとに 3 日に 1 回、古い順に取る（crawl_plan.py）。
  1 件ずつ順に、1.5 秒以上あけて取る。403・429・503 が返ったら、その回は残りを取らない。
- 通信の失敗（net.py が読み直しても届かない、5xx）だけは前回の記録を残し、確認日時も進めない。
  ページは取れたのに料金表がない・読めない（閉店の 404/410、別のページへの転送、作りの変わったページ）ときは、
  料金なしの記録にする。前回の金額を残すと、表が消えた店で古い金額を出し続けるため。
- 営業時間は <br> で切らずに全部を残す（「11:00～23:00<br>金・土・祝前日：11:00～翌5:00」のような曜日別の行がある）。
- 料金表の場所に、見えない要素（display:none など）や既知でない CSS が入っていたら extras に書く。変換器は extras の
  ある店を読まない。
- net.OutOfTime（ジョブの締め切り）で打ち切り、取れた分を保存する。

使い方:
    python scripts/update_utahiro_prices.py
    python scripts/update_utahiro_prices.py --from-cache DIR --out FILE   # 保存済みの uh-N.html から作る（通信しない）
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup, Comment

import crawl_plan
import net
import update_chains

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "source" / "utahiro_prices.json"
SEEN_SOURCE = "source/utahiro_prices.json"
CHAIN = "歌広場"
USER_AGENT = update_chains.USER_AGENT
SCHEMA_VERSION = 1
HOST = "www.utahiro.com"
SHOP_PATH = re.compile(r"^/shops/st_[a-z0-9_]+/\d+$")
INTERVAL = 1.5  # 1 リクエストごとにあける秒数
STOP_CODES = (403, 429, 503)
GONE_CODES = (404, 410)
SAVE_EVERY = 10
# 料金表の場所の CSS。全ページ同じで、中身は空（「/* 外部サイト表示用のCSSがここに入ります。 */ .hoge {}」）
HEADLESS_ID = "ible-headless-styles"
EMPTY_CSS = ".hoge{}"
HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
PRICE_CLASSES = re.compile(r"tab1|tab-content|tab-toggle|tab-menu|db-menu|d-price")
# 文字を取り出すとき、これらの要素の後ろで行を分ける（注記の箇条書きが 1 行につながらないように）
BLOCK_TAGS = ["p", "li", "ul", "ol", "div", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "dt", "dd"]


class Stopped(RuntimeError):
    """公式サイトに止められた（403・429・503）。この回は残りを取らない。"""


def lines_of(node) -> str:
    """要素の文字を、<br> を改行にして返す。行ごとに空白をまとめ、空の行は捨てる。"""
    text = node.get_text("") if node is not None else ""
    lines = (" ".join(line.split()) for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def cores(node) -> list[str]:
    """いちばん外側の ible-part__core ごとの文字（注記の欄は core の中に core が入れ子になっている）。"""
    found = node.select("div.ible-part__core")
    inner = {id(child) for core in found for child in core.select("div.ible-part__core")}
    return [lines_of(core) for core in found if id(core) not in inner]


def images(node) -> list[str]:
    return [img.get("src", "") for img in node.find_all("img")]


def css_is_empty(text: str) -> bool:
    try:
        css = base64.b64decode("".join(text.split()), validate=True).decode("utf-8")
    except (binascii.Error, ValueError):
        return False
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return "".join(css.split()) in ("", EMPTY_CSS)


def hidden(node) -> bool:
    style = node.get("style")
    classes = node.get("class") or []
    return bool((isinstance(style, str) and HIDDEN_STYLE.search(style)) or node.has_attr("hidden")
                or any(name in ("hidden", "d-none", "is-hidden", "u-hidden") for name in classes))


def hours_of(soup: BeautifulSoup) -> str:
    """店舗情報の「営業時間」の欄。<br> の後の曜日別の行も含めて全部を返す。見つからなければ空。"""
    for core in soup.select("div.ible-part__core"):
        if lines_of(core) != "営業時間":
            continue
        part = core.find_parent("div", class_="ible-part__basic")
        value = part.find_next_sibling("div", class_="ible-part__basic") if part else None
        return lines_of(value.select_one("div.ible-part__core")) if value else ""
    return ""


def notices_of(soup: BeautifulSoup) -> list[str]:
    """赤字の告知（p.db-infotext）。<br> で区切られた 1 行を 1 件にする。"""
    found = []
    for paragraph in soup.select("p.db-infotext"):
        found.extend(line for line in lines_of(paragraph).split("\n") if line)
    return found


def price_area(soup: BeautifulSoup, extras: list[str]) -> tuple[list[dict], list[str]]:
    """料金表のタブ（class に tab1 のある ible-grid）を順に読み、帯 → コース → 表 → 見出しと行の並びにする。"""
    bands: list[dict] = []
    tail: list[str] = []
    band = course = table = None
    for grid in soup.select("div.ible-grid"):
        classes = grid.get("class") or []
        if "tab1" not in classes:
            continue
        if grid.find_parent("div", class_="ible-grid") or grid.select_one("div.ible-grid"):
            extras.append("nested-grid")
            continue
        for node in [*grid.parents, grid, *grid.find_all(True)]:
            if node.get("id") == HEADLESS_ID or node.find_parent(id=HEADLESS_ID):
                continue
            if hidden(node):
                extras.append(f"hidden: {' '.join(node.get('class') or [node.name])}"[:120])
        if "tab-toggle" in classes:
            texts = [text for text in cores(grid) if text]
            if len(texts) != 1:
                extras.append(f"band-label: {texts}"[:120])
                band = None
                continue
            band = {"label": texts[0], "courses": [], "images": []}
            bands.append(band)
            course = table = None
            continue
        if "tab-content" not in classes:
            tail.extend(text for text in cores(grid) if text)
            continue
        if band is None:
            extras.append("content-without-band")
            continue
        for block in grid.select("div.ible-block"):
            names = block.get("class") or []
            texts = cores(block)
            if "db-menu-free-img" in names:
                band["images"].extend(images(block))
                if any(texts):
                    extras.append(f"image-text: {texts}"[:120])
            elif "tab-menu-info" in names:
                if block.find("h2"):
                    course = {"name": lines_of(block.find("h2")), "tables": []}
                    band["courses"].append(course)
                    table = None
                elif block.select_one("span.in") and course is not None:
                    table = {"title": lines_of(block.select_one("span.in")), "header": None, "rows": []}
                    course["tables"].append(table)
                else:
                    extras.append(f"menu-info: {texts}"[:120])
            elif "db-menu-tab" in names:
                if table is None:
                    extras.append(f"orphan_row: {texts}"[:120])
                elif "db-menu-tab-th" in names:
                    if table["header"] is not None:
                        extras.append(f"second_header: {texts}"[:120])
                    else:
                        table["header"] = texts
                elif table["header"] is None:
                    extras.append(f"row-before-header: {texts}"[:120])
                else:
                    table["rows"].append(texts)
            elif any(texts) or images(block):
                extras.append(f"block: {' '.join(names)} {texts}"[:120])
    return bands, tail


def parse_page(url: str, markup: str, now: int) -> dict:
    """店のページを記録にする。料金表が見つからなくても例外にせず、bands が空の記録を返す。"""
    soup = BeautifulSoup(markup, "html.parser")
    for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(BLOCK_TAGS):
        block.insert_after("\n")
    extras: list[str] = []
    for node in soup.find_all(id=HEADLESS_ID):
        if not css_is_empty(node.get_text("")):
            extras.append("headless-css")
    for style in soup.find_all("style"):
        if PRICE_CLASSES.search(style.get_text("")):
            extras.append("style-tag")
    bands, tail = price_area(soup, extras)
    return {"page": url, "checked_at": now, "hours": hours_of(soup), "notices": notices_of(soup),
            "bands": bands, "tail": tail, "extras": extras}


def no_prices(url: str, now: int, why: str) -> dict:
    """ページは返ってきたが料金表を読む対象ではない（閉店・転送）。前回の金額は残さない。"""
    return {"page": url, "checked_at": now, "hours": "", "notices": [], "bands": [], "tail": [],
            "extras": [], "missing": why}


def fetch(fetcher: net.Fetcher, url: str, now: int) -> dict:
    """1 店を取る。通信の失敗は requests の例外、止められたら Stopped を投げる。"""
    response = fetcher.get(url, html=True, headers={"Accept-Language": "ja"}, allow_redirects=False)
    status = response.status_code
    if status in STOP_CODES:
        raise Stopped(f"HTTP {status}")
    if status in GONE_CODES:
        return no_prices(url, now, f"HTTP {status}")
    if 300 <= status < 400:
        return no_prices(url, now, f"HTTP {status} {response.headers.get('Location', '')}"[:200])
    if status != 200:
        raise requests.HTTPError(f"HTTP {status}")
    response.encoding = "utf-8"
    return parse_page(url, response.text, now)


def official(url) -> bool:
    parts = urlsplit(url or "")
    return parts.scheme == "https" and parts.hostname == HOST and bool(SHOP_PATH.fullmatch(parts.path))


def load() -> dict:
    if not DATA_FILE.exists():
        return {"schema_version": SCHEMA_VERSION, "updated_at": 0, "shops": {}}
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))


def save(shops: dict, updated_at: int) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": SCHEMA_VERSION, "updated_at": updated_at, "shops": dict(sorted(shops.items()))}
    temporary = DATA_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
                         encoding="utf-8", newline="")
    os.replace(temporary, DATA_FILE)


def candidates(listed: dict, previous: dict) -> list[str]:
    """今回取ってよい店。前回の記録がある店と、新しい店は前回の店数の 4 分の 1 + 10 店まで。

    validate_data.py は元データの店数が前回の 1.25 倍 + 10 を超えると、そのチェーンごと前回に戻す。
    初回が締め切りで途中までしか取れなかったとき、次の回に残りを一度に足すと戻され続けるので、少しずつ足す。
    """
    known = [key for key in listed if key in previous]
    new = [key for key in listed if key not in previous]
    if previous:
        new = new[:len(previous) // 4 + 10]
    return known + new


def update(log=print, *, now: int | None = None, fetcher: net.Fetcher | None = None,
           sleep=time.sleep, clock=time.monotonic) -> bool:
    now = int(time.time()) if now is None else now
    old = load()
    listed = {key: shop for key, shop in update_chains.load_previous().items() if shop.get("chain") == CHAIN}
    if not listed:
        log("歌広場: chains.json に店がないので何もしません")
        return False
    # 閉店して一覧から消えた店の記録は残さない
    shops = {key: record for key, record in old.get("shops", {}).items() if key in listed}
    previous_updated_at = old.get("updated_at", 0)
    # 店ごとに 3 日に 1 回、古い順に取る（crawl_plan.py）。URL が変わった店は毎回取る
    due = crawl_plan.due(candidates(listed, shops), shops.get, SEEN_SOURCE, now,
                         force=lambda key: shops.get(key, {}).get("page") != listed[key].get("url"))
    fetcher = fetcher or net.Fetcher(USER_AGENT, label=CHAIN)
    failed: list[str] = []
    fetched = 0
    last = None
    for index, key in enumerate(due):
        url = listed[key].get("url", "")
        if not official(url):
            log(f"  {key}: 店の URL が歌広場の店のページではありません ({url})")
            failed.append(key)
            continue
        if last is not None:
            sleep(max(0.0, INTERVAL - (clock() - last)))
        last = clock()
        try:
            shops[key] = fetch(fetcher, url, now)
            fetched += 1
        except net.OutOfTime:
            log(f"歌広場: 締め切りを過ぎたので {len(due) - index} 店を取らずに保存します")
            failed.extend(due[index:])
            break
        except Stopped as error:
            log(f"歌広場: 公式サイトに止められたので残り {len(due) - index} 店を取りません ({error})")
            failed.extend(due[index:])
            break
        except (requests.RequestException, ValueError) as error:
            log(f"  {key}: 取得失敗、前回の記録を残します ({type(error).__name__}: {error})"[:300])
            failed.append(key)
            last = clock()
            continue
        last = clock()
        record = shops[key]
        if record.get("missing"):
            log(f"  {key}: 料金表なし ({record['missing']})")
        elif not record["bands"]:
            log(f"  {key}: ページに料金表が見つかりません")
        elif record["extras"]:
            log(f"  {key}: 料金表に見慣れない要素 {record['extras'][:3]}")
        if fetched % SAVE_EVERY == 0:
            save(shops, previous_updated_at)
    save(shops, now if not failed else previous_updated_at)
    priced = sum(bool(record.get("bands")) for record in shops.values())
    log(f"歌広場: 料金表 {priced}/{len(listed)} 店 (今回取得 {fetched}、失敗 {len(failed)}、通信 {fetcher.bytes / 1e6:.1f}MB)")
    return not failed


def from_cache(directory: Path, out: Path, log=print) -> int:
    """保存済みの uh-N.html から記録を作って out に書く（通信しない。変換を試すとき用）。"""
    listed = {key: shop for key, shop in update_chains.load_previous().items() if shop.get("chain") == CHAIN}
    shops = {}
    for path in sorted(directory.glob("uh-*.html")):
        key = path.stem
        if key in listed:
            shops[key] = parse_page(listed[key]["url"], path.read_text(encoding="utf-8"), int(path.stat().st_mtime))
    payload = {"schema_version": SCHEMA_VERSION, "updated_at": int(time.time()), "shops": shops}
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8", newline="")
    log(f"歌広場: 保存済みのページから {len(shops)} 店 → {out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="歌広場の料金表を取得する")
    parser.add_argument("--from-cache", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.from_cache:
        if not args.out:
            parser.error("--from-cache には --out が要ります")
        return from_cache(args.from_cache, args.out, log=lambda line: print(line, flush=True))
    return 0 if update(log=lambda line: print(line, flush=True)) else 1


if __name__ == "__main__":
    sys.exit(main())
