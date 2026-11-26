#!/usr/bin/env python3
"""ビッグエコーの料金表（30 分料金、フリータイム、パック）と営業時間を公式サイトから取得して保存する。

金額への変換は画面側 (web/bigecho.js) の 1 箇所で行うので、ここでは表をそのまま取り出すだけにする。
表のほかに、店舗からのお知らせと、料金欄の中の表のない見出し（「12/1～1/5は特別料金となります」など）も残す。
変換はこれを読んで、料金が変わる期間の告知がある店の金額を出さない。

前回の値を残すのは、通信の失敗（タイムアウト、接続エラー、本文の欠け、5xx、429）と、取れたページがその店の
店舗ページだと確かめられないとき（トップへの転送、料金欄も店の情報もないページ）だけ。どちらも確認日時が
進まないので、14 日たつと publish.py が配らなくなる。
その店の店舗ページだと確かめられたのに表がない店は、その日のうちに表なしで保存する（前回の金額を残さない）。

使い方:
    python update_chain_prices.py                  # 期限の来た店を更新（crawl_plan.py）
    python update_chain_prices.py --from-cache DIR # 保存済みの HTML (be-XXXX.html) から作る
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import requests
from bs4 import BeautifulSoup

import crawl_plan
import net
import update_chains

DATA_FILE = Path(__file__).resolve().parents[1] / "source" / "chain_prices.json"
USER_AGENT = update_chains.USER_AGENT
SCHEMA_VERSION = 1
# 公式サイトへのリクエストの間隔。読み直しも含め、全部のスレッドを合わせて守る。
# 1 秒より少し長くして、スレッドの切り替えで送信が数十ミリ秒ずれても 1 秒を割らないようにする
DELAY = 1.1
PARALLEL = 4  # 利用者の要望で同時に取得する。間隔は DELAY で全体として守る
FETCH_PAUSE = 2.0  # 通信に失敗したときに読み直すまでの待ち時間（net.Fetcher）
THROTTLED_CODES = (429, 503)  # 公式サイトに制限されたら、残りの店は取りに行かない
GONE_CODES = (404, 410)
ATTEMPTS = 2  # 5xx のときの取り直し（接続の失敗は net.Fetcher が読み直す）
SAVE_EVERY = 20
# 保存した表の範囲。30 分料金だけを保存していた頃の店は、日時に関係なく取り直す
SCOPE = "all"
POINT_API = "https://shop.big-echo.jp/api/point/"
ALLOWED_PAGE_HOSTS = ("https://big-echo.jp/", "https://shop.big-echo.jp/")
SHORT_URL = "https://big-echo.jp/?p={}"
# 保存した URL が店舗ページでなくなった店（店名の変更など）は店舗 API で URL を調べ直す。サイトの改修で
# 一度に多くの店が当てはまるとリクエストが倍になるので、1 回の実行ではこの店数までにする
RESOLVE_LIMIT = 5


class Throttled(Exception):
    pass


class NotShopPage(Exception):
    """取れたページがその店の店舗ページだと確かめられない（トップへの転送、料金欄も店の情報もない、別の店のページ）"""


class SiteChanged(Exception):
    """URL を調べ直しても店舗ページだと確かめられない。前回の値を残す（確認日時は進まないので 14 日で配らなくなる）"""


class Gone(Exception):
    """店舗 API にない店。閉店した店として料金なしにする"""


def clean(text: str | None) -> str:
    return " ".join((text or "").split())


def lines_of(node) -> str:
    """改行を残して空白を詰める。お知らせは行ごとに別の話題が並ぶため"""
    lines = (clean(line) for line in node.get_text("\n").splitlines())
    return "\n".join(line for line in lines if line)


def post_id(url: str | None) -> str:
    """「https://big-echo.jp/?p=22446」の 22446。店ごとに決まっていて、店名が変わっても変わらない"""
    if not url:
        return ""
    values = parse_qs(urlsplit(url).query).get("p") or [""]
    return values[0] if values[0].isdigit() else ""


def shop_url(soup: BeautifulSoup) -> str:
    """ページの JSON-LD（LocalBusiness）にある店の URL。なければ空"""
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or "", strict=False)  # 説明文に改行がそのまま入っている
        except ValueError:
            continue
        if isinstance(data, dict):
            data = data.get("@graph", [data])
        for item in data if isinstance(data, list) else []:
            if isinstance(item, dict) and item.get("@type") == "LocalBusiness" and isinstance(item.get("url"), str):
                return item["url"]
    return ""


def dd_after(soup: BeautifulSoup, word: str):
    label = next((dt for dt in soup.find_all("dt") if word in dt.get_text()), None)
    return label.find_next_sibling("dd") if label else None


def parse_soup(soup: BeautifulSoup) -> dict:
    """料金欄の表、表のない見出し、店舗からのお知らせ、営業時間、JSON-LD の店の URL を取り出す"""
    root = soup.select_one("#price")
    sections = []
    notes = []  # 表の付いていない見出し（「※12/1～1/5は特別料金となります。」など）
    if root is not None:
        heading = time_text = ""
        heading_used = time_used = True
        for node in root.select("h3,h4,table"):
            if node.name == "h3":
                if not heading_used and heading:
                    notes.append(heading)
                if not time_used and time_text:
                    notes.append(time_text)
                heading, time_text = clean(node.get_text()), ""
                heading_used = time_used = False
            elif node.name == "h4":
                if not time_used and time_text:
                    notes.append(time_text)
                time_text, time_used = clean(node.get_text()), False
            else:
                rows = [[clean(cell.get_text()) for cell in tr.select("th,td")] for tr in node.select("tr")]
                body = [row for row in rows if len(row) > 1]
                if not body:
                    continue
                heading_used = time_used = True
                single = [row[0] for row in rows if len(row) == 1 and row[0]]
                title = single[0] if single else ""
                sections.append({"heading": heading, "time": time_text, "title": title, "note": " ".join(single[1:]),
                                 "header": body[0], "rows": body[1:]})
        if not heading_used and heading:
            notes.append(heading)
        if not time_used and time_text:
            notes.append(time_text)
        # 見出しと表の外に書かれた文も残す（料金の告知が <p> などで書かれることがある）
        rest = BeautifulSoup(str(root), "html.parser")
        for node in rest.select("h3,h4,table,script,style"):
            node.decompose()
        notes.extend(line for line in lines_of(rest).split("\n") if line)
    hours = dd_after(soup, "営業時間")
    notice = dd_after(soup, "店舗からのお知らせ")
    return {"hours": clean(hours.get_text()) if hours else "", "sections": sections,
            "notice": lines_of(notice) if notice else "", "price_notes": notes,
            "shop_url": shop_url(soup), "price_area": root is not None}


def parse_page(html: str) -> dict | None:
    """料金欄の表、表のない見出し、店舗からのお知らせ、営業時間を取り出す。料金欄がなければ None。"""
    parsed = parse_soup(BeautifulSoup(html, "html.parser"))
    if not parsed.pop("price_area"):
        return None
    parsed.pop("shop_url")
    return parsed


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


class Pacer:
    """全部のスレッドを合わせて、リクエストの間を DELAY 秒以上あける"""

    def __init__(self):
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + DELAY
        if start > now:
            time.sleep(start - now)


class PacedFetcher(net.Fetcher):
    """net.Fetcher の読み直しも含めて、すべてのリクエストの前に Pacer で間をあける"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pacer = Pacer()

    def session(self) -> requests.Session:
        self.pacer.wait()
        return super().session()


class Budget:
    """1 回の実行で URL を調べ直す店の数"""

    def __init__(self, limit: int):
        self._lock = threading.Lock()
        self.left = limit

    def take(self) -> bool:
        with self._lock:
            if self.left <= 0:
                return False
            self.left -= 1
            return True


def check(response: requests.Response) -> None:
    if response.status_code in THROTTLED_CODES:
        raise Throttled(f"HTTP {response.status_code}")
    response.raise_for_status()


def resolve(fetcher: net.Fetcher, shop_id: str) -> str:
    """店舗 API から店舗ページの短縮 URL を調べる"""
    response = fetcher.get(POINT_API + shop_id.removeprefix("be-"))
    if response.status_code in GONE_CODES:
        raise Gone(f"店舗 API にない店 (HTTP {response.status_code})")
    check(response)
    url = response.json()["短縮ＵＲＬ"]
    if not isinstance(url, str) or not url.startswith(ALLOWED_PAGE_HOSTS):
        raise ValueError(f"想定外の接続先: {url}")
    return url


def same_shop(found: str, expected: str, final: str, requested: str, via_api: bool, price_area: bool) -> bool:
    """取れたページがその店の店舗ページか。found は JSON-LD の店の番号、expected は保存した短縮 URL の番号"""
    if found and expected:
        return found == expected  # 店名の付け替えなどで、保存した URL が別の店のページになっていないか
    if via_api:
        return bool(found) or price_area  # 店舗 API が教えた URL から、公式サイトの中だけで転送された
    if unquote(final) != unquote(requested):
        return False  # 保存した URL から別のページへ転送された（店名の変更でトップへ転送など）
    return bool(found) or price_area


def read_page(fetcher: net.Fetcher, url: str, expected: str, *, via_api: bool = False) -> dict:
    """店舗ページを読む。その店の店舗ページだと確かめられなければ NotShopPage。"""
    response = fetcher.get(url, html=True)
    if response.status_code in GONE_CODES:
        raise NotShopPage(f"HTTP {response.status_code}")
    check(response)
    response.encoding = "utf-8"
    final = response.url.replace("http://", "https://", 1)
    if not final.startswith(ALLOWED_PAGE_HOSTS):
        raise NotShopPage(f"公式サイトの外へ転送 ({final})")
    parsed = parse_soup(BeautifulSoup(response.text, "html.parser"))
    found = post_id(parsed.pop("shop_url"))
    price_area = parsed.pop("price_area")
    if not same_shop(found, post_id(expected), final, url, via_api, price_area):
        raise NotShopPage(f"別の店のページ (?p={found})" if found and post_id(expected) else f"店舗ページではない ({final})")
    # 店の番号（短縮 URL）を覚えて、次回から JSON-LD と比べる。転送の先を覚えて、次回から 1 回の通信で済ませる
    number = found or post_id(expected)
    return {**parsed, "page": final, **({"short": SHORT_URL.format(number)} if number else {}),
            "scope": SCOPE, "checked_at": int(time.time())}


def gone_record(record: dict) -> dict:
    """店舗 API にない店の記録。前回の金額は残さない"""
    kept = {key: record[key] for key in ("page", "short") if record.get(key)}
    return {"hours": "", "sections": [], "notice": "", "price_notes": [], **kept, "scope": SCOPE,
            "checked_at": int(time.time())}


def fetch_shop(fetcher: net.Fetcher, shop_id: str, record: dict, budget: Budget, log=print) -> dict:
    """1 店を読む。店舗ページだと確かめられなければ SiteChanged。"""
    known = record.get("page")
    if known:
        try:
            # 店の番号は保存した短縮 URL から。古い記録で page が「?p=N」の形ならその番号を使う
            return read_page(fetcher, known, record.get("short") or (known if post_id(known) else ""))
        except NotShopPage as reason:
            if not budget.take():
                raise SiteChanged(f"{reason}。URL の調べ直しは今回の上限 ({RESOLVE_LIMIT} 店) に達しました") from reason
            log(f"  {shop_id}: 保存した URL が店舗ページではないので、店舗 API で調べ直します ({reason})")
    try:
        url = resolve(fetcher, shop_id)
    except Gone as reason:
        log(f"  {shop_id}: {reason}。料金なしにします")
        return gone_record(record)
    try:
        return read_page(fetcher, url, url, via_api=True)
    except NotShopPage as reason:
        raise SiteChanged(str(reason)) from reason


def has_tables(shops: dict[str, dict]) -> int:
    return sum(1 for shop in shops.values() if shop.get("sections"))


def update(log=print) -> bool:
    """期限の来た店を更新して保存する。全店を読めたら True。通信に失敗した店は前回の値を残す。

    締め切り（CRAWL_DEADLINE）を過ぎたら残りの店は取らずに、そこまでに取れた分を保存して終わる。
    """
    previous = load_previous()
    shops = dict(previous)
    fetcher = PacedFetcher(USER_AGENT, label="ビッグエコー", pause=FETCH_PAUSE)
    budget = Budget(RESOLVE_LIMIT)
    failed = 0
    done = 0
    ids = shop_ids()
    # 店ごとに 3 日に 1 回、古い順に取る（crawl_plan.py）。表の範囲が古い店は毎回取る
    due = crawl_plan.due(ids, shops.get, "source/chain_prices.json", int(time.time()),
                         force=lambda shop_id: shops.get(shop_id, {}).get("scope") != SCOPE)
    stop = threading.Event()
    late = threading.Event()

    def fetch(shop_id: str) -> tuple[str, dict | None, str]:
        if stop.is_set():
            return shop_id, None, ""
        error = ""
        for _ in range(ATTEMPTS):
            try:
                return shop_id, fetch_shop(fetcher, shop_id, shops.get(shop_id, {}), budget, log), ""
            except net.OutOfTime:
                late.set()
                stop.set()
                return shop_id, None, ""
            except Throttled as limit:
                stop.set()
                return shop_id, None, f"公式サイトの制限で中止 ({limit})"
            except SiteChanged as reason:
                return shop_id, None, f"店舗ページだと確かめられないので前回の値を残します ({reason})"
            except requests.HTTPError as failure:
                error = f"HTTP の失敗 ({failure})"  # 一時的な 502 などがあるので取り直す
            except requests.RequestException as failure:
                return shop_id, None, f"通信の失敗 ({type(failure).__name__}: {failure})"
            except (ValueError, KeyError, TypeError) as failure:
                return shop_id, None, f"店舗 API の応答を読めません ({type(failure).__name__}: {failure})"
        return shop_id, None, error

    before = {shop_id: bool(shops.get(shop_id, {}).get("sections")) for shop_id in due}
    skipped = 0
    # 取得だけを同時に行い、結果はこのスレッドで順に反映する
    with ThreadPoolExecutor(PARALLEL) as pool:
        for shop_id, shop, error in pool.map(fetch, due):
            if shop is None:
                if error:
                    log(f"  取得失敗 {shop_id} ({error})")
                    failed += 1
                else:
                    skipped += 1
                continue
            shops[shop_id] = shop
            done += 1
            if done % SAVE_EVERY == 0:
                save(shops, last_updated())
    if late.is_set():
        log(f"締め切りを過ぎたので、残りの {skipped} 店は次の回に取ります")
    elif skipped:
        log(f"公式サイトの制限で {skipped} 店は取りませんでした")
    # 閉店した店は残さない
    shops = {shop_id: shops[shop_id] for shop_id in ids if shop_id in shops}
    lost = [shop_id for shop_id, had in before.items() if had and shop_id in shops and not shops[shop_id].get("sections")]
    if lost:
        # 表を外した店は料金なしになる（年末年始などに表を外す店がある）。誤った金額にはならないので止めない
        log(f"表がなくなった店 {len(lost)} 店: {', '.join(lost[:20])}")
    # 失敗した店や、締め切りで取れなかった店があれば「成功した更新日時」は進めない
    complete = not failed and not skipped
    save(shops, int(time.time()) if complete else last_updated())
    log(f"ビッグエコー: {has_tables(shops)}/{len(ids)} 店 (今回 {done} 店、失敗 {failed}、未取得 {skipped})")
    return not failed


def from_cache(directory: Path) -> None:
    now = int(time.time())
    shops = {}
    for shop_id in shop_ids():
        path = directory / f"{shop_id}.html"
        if not path.exists():
            continue
        parsed = parse_soup(BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser"))
        found = post_id(parsed.pop("shop_url"))
        if parsed.pop("price_area"):
            shops[shop_id] = {**parsed, **({"short": SHORT_URL.format(found)} if found else {}),
                              "scope": SCOPE, "checked_at": now}
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
