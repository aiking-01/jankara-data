#!/usr/bin/env python3
"""まねきねこの料金表 PDF の文字を座標つきで取り出して保存する。金額への変換は web/maneki.js が行う。

PDF は複数ページのものが多い。料金表のページは文字を座標つきで残す。全ページの文（特別料金や年末年始の告知など）と、
ページごとの文字・図形・画像の量も残し、料金表のページと画像が中心のページには見た目の指紋（look）を付ける。店のページのお知らせ、
全店共通でない料金の注記、特別営業時間の欄も残す。変換は告知の期間や画像のページを見て、金額を出すかを決める。
店の URL は /locations/ の「もっと見る」と同じ一覧から得る。

robots.txt は PDF の置き場所 /wp-content/uploads/ を巡回の対象外としているが、利用者の判断で全店を確認する。
負荷を抑えるため 1 秒に 1 件ずつ順に取得し、店のページの更新日時が変わった店の PDF だけを取る。
更新日時が同じでも、PDF を最後に確かめてから 5 日以上たっていれば（3 日ごとの巡回なら 6 日ごと）、店のページを最後まで
読み、PDF を If-None-Match で確かめる（同じ URL のまま PDF を差し替えたり、お知らせだけを足したりしても見逃さないため。
変わっていなければ 304 で本文は来ない。304 が返らないと分かったら、その回の残りは HEAD で比べる）。

告知を残すようにする前の記録（版が EXTRACT より古い）は変換が読まない。1 回に BACKFILL 店ずつ、最後に確かめたのが
古い順に PDF を取り直して埋める（全店を一度に取り直さない）。

通信の失敗（タイムアウト、接続エラー、本文の欠け、5xx）の店は前回の記録を残す（確かめた日時は進まないので、
14 日たてば publish.py が配らなくなる）。ページは取れたのに料金表が読めない店（PDF がない、2 つ以上ある、
PDF が消えた、壊れている、文字がない）は、その日のうちに料金なしの記録にする。閉店（店のページが 404/410）は
失敗に数えず、料金なしの記録にする（店の一覧から消えたら記録も消える）。
CRAWL_DEADLINE を過ぎたら残りの店は取らずに、そこまでの分を保存して正常に終える。

使い方:
    python update_maneki_prices.py                  # 3 日に 1 回の割り当てぶんを更新
    python update_maneki_prices.py --from-cache DIR # 保存済みの mn-*.html と mn-*.pdf から作る
"""
from __future__ import annotations

import argparse
import html as html_lib
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import NamedTuple

import requests

import crawl_plan
import net
import seen
import update_chains

try:
    import pypdfium2
    import pypdfium2.raw as pdfium_c
except ImportError:  # PC 版は PDF を読めないときも他のチェーンの更新を続ける
    pypdfium2 = None

DATA_FILE = Path(__file__).resolve().parents[1] / "source" / "maneki_prices.json"
SOURCE = "source/maneki_prices.json"
USER_AGENT = update_chains.USER_AGENT
SCHEMA_VERSION = 1
# 取り出しの版。告知と各ページの統計を残すようにしたので 2。web/maneki.js の EXTRACT_VERSION と同じ値にする
EXTRACT = 2
BACKFILL = 50
PDF_RECHECK = 5 * 86400
DELAY = 1.0
THROTTLED_CODES = (403, 429, 503)
PDF_THROTTLED = (429, 503)  # PDF の置き場所（S3）は、ない PDF に 403 を返すことがある
GONE = (404, 410)
PDF_GONE = (403, 404, 410)
MIN_RATIO = 0.8
SAVE_EVERY = 20
CHUNK = 4096
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_TEXT = 6000  # 1 つの文の長さ。これより長ければ切って印を付け、変換は読まない
OPTION_HEAD = "オプション"
FOOTNOTE_START = "※ご利用は"
NOISE = ("円", "~", "～")
# 画像が中心のページ（web/maneki.js の isVisual と同じ条件）
VISUAL_AREA = 0.15
MODIFIED = re.compile(rb'article:modified_time" content="([^"]+)"')
PDF_URL = re.compile(r"https://www\.karaokemanekineko\.jp/wp-content/uploads/[^\"'\s<>]+\.pdf")
NEWS_ITEM = re.compile(r'<li class="storeDetail__info__news__item"[^>]*>(.*?)</li>', re.S)
NEWS_DATE = re.compile(r'<time datetime="(\d{4}-\d{2}-\d{2})"')
NEWS_BODY = re.compile(r'class="storeDetail__info__news__body"[^>]*>(.*?)</p>', re.S)
REMARKS = re.compile(r'class="storeDetail__price__remarks"[^>]*>(.*?)</ul>', re.S)
REMARK = re.compile(r"<li[^>]*>(.*?)</li>", re.S)
# 特別営業時間の欄（ふだんは空）。中身の作りが分からないので、次の欄までの文をそのまま残す
SPECIAL_HOURS = re.compile(r'class="storeDetail__info__specialHours__wrap[^"]*"[^>]*>(.*?)<div class="storeDetail__wrap"', re.S)
# 料金表の下の注記は全店で同じ文。これ以外の文が載ったときだけ告知として残す
STANDARD_REMARKS = {
    "朝うた料金は、30分あたりの金額となります。",
    "上記料金表および料金は予告なく変更になる場合がございます。",
    "上記以外の料金プランは、ご利用の店舗へお問い合わせください。",
    "ワンドリンクオーダー制は、室料とは別に451円(税込)以上のドリンクを1品ご注文いただきます。詳しくは店舗へお問合せください。",
    "カラオケまねきねこは全店会員制でございます。入会の際、指定の身分証明書(運転免許証・健康保険証など)の確認が必要となりますのでご持参ください。(会員証は、全国の店舗にてご利用いただけます。)",
    "ご利用は1時間からとなります。(延長は30分単位で可能)",
    "ゴールデンウィーク・夏休み期間・年末年始などの特別期間は料金が異なる場合がございます。",
    "混雑時には、ご利用時間を制限させていただく場合がございます。(フリータイムを含む。)",
    "お一人様でご利用の場合は、料金が異なる場合がございます。詳しくは店舗へお問い合わせください。",
}
# PDF から取り出したもの。PDF が変わっていなければ（304）前回の値を使う。版（extract）も PDF とともに残す
PDF_FIELDS = ("pdf", "etag", "last_modified", "length", "extract", "tables", "notices", "pages", "status")


class Throttled(Exception):
    pass


class Page(NamedTuple):
    status: int
    modified: str
    html: str | None  # 更新日時が前回と同じで途中で切ったときは None
    size: int


class PageFetcher(net.Fetcher):
    """店のページは、更新日時が前回と同じなら先頭の数 KB で切る。net.Fetcher.get は本文を全部読むので、ここだけ流し読みする。"""

    def read_page(self, url: str, known: str | None) -> Page:
        last: Exception | None = None
        for attempt in range(self.attempts):
            net.check_deadline()
            time.sleep(DELAY)
            session = self.session()
            try:
                with session.get(url, timeout=self.timeout, stream=True) as response:
                    if response.status_code in THROTTLED_CODES:
                        raise Throttled(f"HTTP {response.status_code}")
                    if response.status_code in GONE:
                        return Page(response.status_code, "", None, 0)
                    response.raise_for_status()  # 5xx などは通信の失敗として前回の記録を残す
                    body = bytearray()
                    modified = None
                    for chunk in response.iter_content(CHUNK):
                        body += chunk
                        if modified is None and (match := MODIFIED.search(body)):
                            modified = match.group(1).decode("ascii", "replace")
                            if known and modified == known:
                                self._read(len(body))
                                return Page(200, modified, None, len(body))
                    if b"</html>" not in bytes(body[-4096:]).lower():
                        raise net.Incomplete(f"本文が途中までしか届いていません ({len(body)} バイト)")
                    self._read(len(body))
                    return Page(200, modified or "", body.decode("utf-8", "replace"), len(body))
            except requests.exceptions.ProxyError as error:
                if "407" in str(error):
                    raise
                last = error
            except (requests.ConnectionError, requests.Timeout) as error:
                last = error
            finally:
                session.close()
            self._failed(last)
            if attempt + 1 < self.attempts:
                time.sleep(self.pause * (attempt + 1))
        assert last is not None
        raise last

    def _read(self, size: int) -> None:
        with self._lock:
            self.failures = 0
            self.bytes += size


def char_runs(page) -> list[list]:
    """ページの文字を、空白で切れ目のない並びごとにまとめる。"""
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


def page_stats(page) -> dict:
    """文字をアウトライン化した図形や、画像だけの告知のページを見分けるための量。image_area は最も大きい画像の面積の割合"""
    width, height = page.get_size()
    paths = images = 0
    area = 0.0
    for item in page.get_objects():
        if item.type == pdfium_c.FPDF_PAGEOBJ_PATH:
            paths += 1
        elif item.type == pdfium_c.FPDF_PAGEOBJ_IMAGE:
            images += 1
            left, bottom, right, top = item.get_bounds()
            area = max(area, (right - left) * (top - bottom) / (width * height))
    return {"paths": paths, "images": images, "image_area": round(min(max(area, 0.0), 1.0), 3)}


def visual(stats: dict) -> bool:
    return not stats["table"] and (stats["image_area"] >= VISUAL_AREA
                                   or (stats["chars"] < 20 and (stats["paths"] >= 100 or stats["images"] >= 10)))


def look(page) -> str:
    """ページの見た目の指紋（縦 8 × 横 9 に縮めた明るさの差の 64 ビット）。同じ案内の画像は、作り直した PDF でもほぼ同じ値になる"""
    width, height = page.get_size()
    bitmap = page.render(scale=96 / max(width, height), grayscale=True)
    columns, rows = 9, 8
    pixels, stride = bytes(bitmap.buffer), bitmap.stride
    grid = []
    for row in range(rows):
        y0 = row * bitmap.height // rows
        y1 = max((row + 1) * bitmap.height // rows, y0 + 1)
        line = []
        for column in range(columns):
            x0 = column * bitmap.width // columns
            x1 = max((column + 1) * bitmap.width // columns, x0 + 1)
            total = sum(pixels[y * stride + x] for y in range(y0, y1) for x in range(x0, x1))
            line.append(total / ((y1 - y0) * (x1 - x0)))
        grid.append(line)
    bits = 0
    for line in grid:
        for column in range(columns - 1):
            bits = bits << 1 | (line[column] > line[column + 1])
    return f"{bits:016x}"


def page_text(runs: list[list]) -> str:
    return unicodedata.normalize("NFKC", " ".join(run[0] for run in runs))


def extract(pdf: bytes | str | Path) -> dict:
    """料金表のページの文字（座標つき）、全ページの文、ページごとの量を返す。文字のない PDF は tables が空。"""
    document = pypdfium2.PdfDocument(pdf if isinstance(pdf, bytes) else str(pdf))
    tables, notices, pages = [], [], []
    try:
        for index in range(len(document)):
            page = document[index]
            runs = char_runs(page)
            table = is_price_table(runs)
            if table:
                tables.append(keep_tokens(runs))
            text = page_text(runs)
            if text:
                notice = {"page": index + 1, "table": table, "text": text[:MAX_TEXT]}
                if len(text) > MAX_TEXT:
                    notice["cut"] = True
                notices.append(notice)
            stats = {"table": table, "chars": sum(len(run[0]) for run in runs), **page_stats(page)}
            # 料金表のページの見た目も残す。変換は、料金表に似た画像のページ（画像にした料金表）を見分けるのに使う
            if table or visual(stats):
                stats["look"] = look(page)
            pages.append(stats)
    finally:
        document.close()
    return {"tables": tables, "notices": notices, "pages": pages}


def tables_of(pdf: bytes | str | Path) -> list[list]:
    """料金表のページの文字をページごとに返す。文字のない PDF は空。"""
    return extract(pdf)["tables"]


def _clean(markup: str) -> str:
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", markup))
    return " ".join(unicodedata.normalize("NFKC", text).split())


def store_notices(markup: str) -> list[dict]:
    """店のページのお知らせ、特別営業時間の欄、全店共通でない料金の注記。date はお知らせの掲載日（ほかは空）。"""
    notices = []
    for item in NEWS_ITEM.findall(markup):
        date = NEWS_DATE.search(item)
        body = NEWS_BODY.search(item)
        text = _clean(body.group(1) if body else item)
        if text:
            notices.append({"date": date.group(1) if date else "", "text": text[:MAX_TEXT], **({"cut": True} if len(text) > MAX_TEXT else {})})
    special = SPECIAL_HOURS.search(markup)
    text = _clean(special.group(1)) if special else ""
    if text:
        notices.append({"date": "", "text": text[:MAX_TEXT], **({"cut": True} if len(text) > MAX_TEXT else {})})
    remarks = REMARKS.search(markup)
    for item in REMARK.findall(remarks.group(1) if remarks else ""):
        text = _clean(item)
        if text and text.replace(" ", "") not in STANDARD_REMARKS:
            notices.append({"date": "", "text": text[:MAX_TEXT]})
    return notices


def pdf_links(markup: str) -> list[str]:
    return list(dict.fromkeys(PDF_URL.findall(markup)))


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


def _same_tag(first: str | None, second: str | None) -> bool:
    plain = lambda tag: (tag or "").removeprefix("W/")
    return bool(first) and plain(first) == plain(second)


class PdfReader:
    """PDF を取る。前回と同じ PDF なら本文は受け取らない（If-None-Match → 304）。

    304 が返らない（送った ETag と同じ ETag で 200 が返る）と分かったら、その回の残りは HEAD の
    ETag、Last-Modified、Content-Length で比べてから取る。
    """

    def __init__(self, fetcher: net.Fetcher, log=print):
        self.fetcher = fetcher
        self.log = log
        self.use_head = False
        self.downloads = 0
        self.unchanged = 0

    def read(self, url: str, old: dict | None) -> tuple[str, requests.Response | None]:
        """("same", None)、("gone", None)、("new", 応答) のどれかを返す。old は同じ URL の前回の記録（なければ本文ごと取る）"""
        if old and self.use_head and self._unchanged_by_head(url, old):
            self.unchanged += 1
            return "same", None
        time.sleep(DELAY)
        headers = {"If-None-Match": old["etag"]} if old and old.get("etag") else None
        response = self.fetcher.get(url, pdf=True, headers=headers)
        if response.status_code == 304:
            self.unchanged += 1
            return "same", None
        if response.status_code in PDF_THROTTLED:
            raise Throttled(f"HTTP {response.status_code}")
        if response.status_code in PDF_GONE:
            return "gone", None
        response.raise_for_status()
        self.downloads += 1
        if headers and _same_tag(response.headers.get("ETag"), old.get("etag")) and not self.use_head:
            self.use_head = True
            self.log("PDF の If-None-Match に 304 が返らないので、この回の残りは HEAD で比べます")
        return "new", response

    def _unchanged_by_head(self, url: str, old: dict) -> bool:
        net.check_deadline()
        time.sleep(DELAY)
        session = self.fetcher.session()
        try:
            response = session.head(url, timeout=self.fetcher.timeout, allow_redirects=True)
        finally:
            session.close()
        if response.status_code in PDF_THROTTLED:
            raise Throttled(f"HTTP {response.status_code}")
        if response.status_code != 200:
            return False
        headers = response.headers
        if not _same_tag(headers.get("ETag"), old.get("etag")):
            return False
        if old.get("last_modified") and headers.get("Last-Modified") != old["last_modified"]:
            return False
        # HEAD は圧縮しない長さを返す。記録の length も展開した本文の長さ
        if old.get("length") and not headers.get("Content-Encoding") and headers.get("Content-Length") != str(old["length"]):
            return False
        return True


def fetch_shop(fetcher: PageFetcher, reader: PdfReader, url: str, old: dict, *, refresh: bool = False) -> tuple[dict, int]:
    """1 店を確かめて、保存する内容と通信量を返す。通信の失敗は例外（呼び出し側で前回の記録を残す）。

    refresh は古い版の記録を取り直す店。ページを最後まで読み、PDF も本文ごと取り直す。
    """
    now = int(time.time())
    current = old.get("extract") == EXTRACT
    # PDF を最後に確かめた日時。時刻だけの変更として select_changes.py と validate_data.py が無視するよう、入れ子の checked_at に置く
    check = old.get("pdf_check")
    checked = check.get("checked_at", 0) if isinstance(check, dict) else 0
    full = refresh or not old.get("modified") or (current and now - checked >= PDF_RECHECK)
    page = fetcher.read_page(url, None if full else old.get("modified"))
    if page.status in GONE:
        # 閉店した店。失敗には数えず、料金なしの記録にする（店の一覧から消えたら記録も消える）
        return {"status": "closed", "checked_at": now, "extract": EXTRACT, "tables": []}, page.size
    if page.html is None:
        return {**old, "checked_at": now}, page.size
    links = pdf_links(page.html)
    record = {"modified": page.modified, "checked_at": now, "pdfs": len(links), "news": store_notices(page.html),
              "pdf_check": {"checked_at": now}}
    if len(links) != 1:
        # 料金表の PDF が 2 つ以上あれば、どれが通常の料金か分からない（期間限定の PDF を通常料金として読まない）
        return {**record, "extract": EXTRACT, "status": "several-pdfs" if links else "no-pdf", "tables": []}, page.size
    pdf = links[0]
    # 前回と同じ PDF なら If-None-Match で確かめる。古い版の記録も、取り直しの割り当て（refresh）でなければ同じ
    same_pdf = not refresh and pdf == old.get("pdf") and not old.get("status") and isinstance(old.get("tables"), list)
    result, response = reader.read(pdf, old if same_pdf else None)
    if result == "same":
        return {**record, **{key: old[key] for key in PDF_FIELDS if key in old}}, page.size
    if result == "gone":
        return {**record, "extract": EXTRACT, "pdf": pdf, "status": "pdf-missing", "tables": []}, page.size
    body = response.content
    record.update({"pdf": pdf, "etag": response.headers.get("ETag"),
                   "last_modified": response.headers.get("Last-Modified", ""), "length": len(body), "extract": EXTRACT})
    size = page.size + len(body)
    if body[:4] != b"%PDF" or len(body) > MAX_PDF_BYTES:
        return {**record, "status": "unreadable-pdf", "tables": []}, size
    try:
        return {**record, **extract(body)}, size
    except pypdfium2.PdfiumError:
        return {**record, "status": "unreadable-pdf", "tables": []}, size


def readable(shops: dict[str, dict]) -> int:
    return sum(1 for shop in shops.values() if shop.get("tables"))


def accept(previous: dict[str, dict], shops: dict[str, dict]) -> bool:
    return readable(shops) >= readable(previous) * MIN_RATIO


def backfill(pages: dict[str, str], shops: dict[str, dict], state: dict) -> set[str]:
    """古い版の記録のうち BACKFILL 店。告知を埋めるため PDF ごと取り直す。

    料金表の文字がある店（埋めれば配れる店）を先に、その中で最後に確かめたのが古い順に選ぶ。
    """
    old = [shop_id for shop_id in pages if shop_id in shops and shops[shop_id].get("extract") != EXTRACT]
    old.sort(key=lambda shop_id: (not shops[shop_id].get("tables"), seen.last_seen(state, SOURCE, shop_id, shops[shop_id])))
    return set(old[:BACKFILL])


def update(log=print) -> bool:
    """更新して保存する。通信に失敗した店は前回の値を残す。失敗がなければ True（締め切りで打ち切ったときも True）。"""
    if pypdfium2 is None:
        log("pypdfium2 がないのでまねきねこの料金を更新できません")
        return False
    previous = load_previous()
    shops = dict(previous)
    fetcher = PageFetcher(USER_AGENT, label="まねきねこ")
    reader = PdfReader(fetcher, log)
    pages = shop_pages()
    now = int(time.time())
    refresh = backfill(pages, shops, seen.load())
    # 店ごとに 3 日に 1 回、古い順に取る（crawl_plan.py）。古い版の記録の取り直しは割り当ての中で先に取る
    due = crawl_plan.due(list(pages), shops.get, SOURCE, now, force=refresh.__contains__)
    failed = closed = 0
    total = 0
    finished = True
    try:
        for number, shop_id in enumerate(due, 1):
            try:
                shops[shop_id], size = fetch_shop(fetcher, reader, pages[shop_id], shops.get(shop_id, {}),
                                                  refresh=shop_id in refresh)
                total += size
                closed += shops[shop_id].get("status") == "closed"
            except (Throttled, net.OutOfTime):
                raise
            except Exception as failure:  # 1 店の異常で残りを止めない。前回の記録を残す
                log(f"  取得失敗 {shop_id} ({type(failure).__name__}: {failure})")
                failed += 1
            if number % SAVE_EVERY == 0 and accept(previous, shops):
                save(shops, last_updated())
    except Throttled as stop:
        log(f"公式サイトに制限されたので中止します ({stop})")
        if accept(previous, shops):
            save(shops, last_updated())
        return False
    except net.OutOfTime:
        log("締め切りを過ぎたので、残りの店は次の回に取ります")
        finished = False
    shops = {shop_id: shops[shop_id] for shop_id in pages if shop_id in shops}
    if not accept(previous, shops):
        log(f"文字を読めた店が異常に少ないので前回の値を残します ({readable(previous)} → {readable(shops)})")
        return False
    save(shops, int(time.time()) if finished and not failed else last_updated())
    stale = sum(1 for shop in shops.values() if shop.get("extract") != EXTRACT)
    log(f"まねきねこ: {readable(shops)}/{len(pages)} 店 (確認 {len(due)}, 失敗 {failed}, 閉店 {closed}, "
        f"PDF 取得 {reader.downloads}, 304 {reader.unchanged}, 古い版の記録 {stale}, 通信 {total / 1e6:.1f}MB)")
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
        modified = MODIFIED.search(html.encode())
        row = index.get(shop_id, {})
        links = pdf_links(html)
        shops[shop_id] = {
            "modified": modified.group(1).decode() if modified else "",
            "checked_at": now,
            "pdfs": len(links),
            "news": store_notices(html),
            "pdf": row.get("url") or (links[0] if links else ""),
            "etag": row.get("etag"),
            "length": path.stat().st_size,
            "extract": EXTRACT,
            "pdf_check": {"checked_at": now},
            **extract(path),
        }
        if len(links) != 1:
            shops[shop_id].update({"status": "several-pdfs" if links else "no-pdf", "tables": []})
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
