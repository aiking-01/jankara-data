"""まねきねこの取得（update_maneki_prices.py）と店一覧のページ送り（update.py の maneki_urls）。通信はしない。"""
import ctypes
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import net  # noqa: E402
import update_maneki_prices as maneki  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update.py"
spec = importlib.util.spec_from_file_location("karaoke_update_for_maneki", SCRIPT)
update_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = update_module
spec.loader.exec_module(update_module)

MODIFIED = "2026-10-16T11:10:02+00:00"
PDF = "https://www.karaokemanekineko.jp/wp-content/uploads/2025/11/16200950/623.pdf"
OTHER_PDF = "https://www.karaokemanekineko.jp/wp-content/uploads/2025/11/20151317/819-20.pdf"
PDF_BODY = b"%PDF-1.7 test body\n%%EOF\n"
EXTRACTED = {"tables": [[["30分", 1, 2, 3, 4]]], "notices": [{"page": 1, "table": True, "text": "30分 会員"}],
             "pages": [{"table": True, "chars": 5, "paths": 0, "images": 0, "image_area": 0.0, "look": "0" * 16}]}
NEWS = ('<li class="storeDetail__info__news__item" data-info-type="type_info"><div class="storeDetail__info__news__meta">'
        '<time datetime="2026-11-20" class="storeDetail__info__news__date">2026/11/20</time></div>'
        '<p class="storeDetail__info__news__body"> 12/29～1/3は<br />年末年始特別料金です </p></li>')


def page_html(links=(PDF,), modified=MODIFIED, news=""):
    head = f'<html><head><meta property="article:modified_time" content="{modified}" /></head><body>'
    filler = " " * 9000  # 更新日時は先頭の 1 チャンク目にある
    anchors = "".join(f'<a href="{link}" class="storeDetail__price__button__link">' for link in links)
    return (head + filler + anchors + f'<ul class="storeDetail__info__news__list">{news}</ul></body></html>').encode()


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None):
        self.status_code = status
        self.content = body
        self.headers = requests.structures.CaseInsensitiveDict(headers or {})

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def iter_content(self, size):
        for start in range(0, len(self.content), size):
            yield self.content[start:start + size]


class FakeSite:
    """店のページと PDF を返す。受けたリクエストを記録する。"""

    def __init__(self, page=None, pdf=None, head=None):
        self.page = page if isinstance(page, FakeResponse) else FakeResponse(body=page or page_html())
        self.pdf = pdf or FakeResponse(body=PDF_BODY, headers={"ETag": 'W/"new"', "Last-Modified": "Mon, 23 Nov 2026 23:11:14 GMT"})
        self.head = head
        self.calls = []  # (method, url, headers)
        self.read = 0

    def session(self):
        site = self

        class Session:
            def get(self, url, timeout=None, stream=False, headers=None, allow_redirects=True):
                site.calls.append(("GET", url, headers))
                if url.endswith(".pdf"):
                    return site.pdf
                return site.page

            def head(self, url, timeout=None, allow_redirects=True):
                site.calls.append(("HEAD", url, None))
                return site.head

            def close(self):
                pass

        return Session()

    def pdf_calls(self):
        return [(method, headers) for method, url, headers in self.calls if url.endswith(".pdf")]


def fetcher_for(site):
    fetcher = maneki.PageFetcher("test", label="test")
    fetcher.session = site.session
    return fetcher


class Base(unittest.TestCase):
    def setUp(self):
        for patcher in (mock.patch.object(maneki.time, "sleep"), mock.patch.object(net.time, "sleep"),
                        mock.patch.object(maneki, "extract", return_value=EXTRACTED),
                        mock.patch.dict(os.environ, {"CRAWL_DEADLINE": ""})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def fetch(self, site, old, **kwargs):
        fetcher = fetcher_for(site)
        reader = maneki.PdfReader(fetcher, log=lambda line: None)
        return maneki.fetch_shop(fetcher, reader, "https://x/", old, **kwargs)


def current(**extra):
    """告知を残すようにした後の記録"""
    return {"modified": MODIFIED, "pdf": PDF, "etag": 'W/"a"', "length": 10, "extract": maneki.EXTRACT,
            "pdf_check": {"checked_at": int(time.time())}, "pdfs": 1, "news": [], "tables": [["keep"]],
            "notices": [], "pages": [], "checked_at": 1, **extra}


class FetchShopTest(Base):
    def test_same_modified_time_stops_early_and_skips_pdf(self):
        site = FakeSite()
        shop, size = self.fetch(site, current())
        self.assertEqual(site.pdf_calls(), [])
        self.assertLessEqual(size, maneki.CHUNK)
        self.assertEqual(shop["tables"], [["keep"]])
        self.assertGreater(shop["checked_at"], 1)

    def test_pdf_is_checked_with_if_none_match_every_third_crawl_even_if_the_page_is_unchanged(self):
        # 3 日ごとの巡回の 2 回目（6 日）はページの先頭だけ
        site = FakeSite(page=page_html(news=NEWS), pdf=FakeResponse(status=304))
        shop, size = self.fetch(site, current(pdf_check={"checked_at": int(time.time()) - 6 * 86400}))
        self.assertEqual(site.pdf_calls(), [])
        self.assertLessEqual(size, maneki.CHUNK)
        # 3 回目（9 日）はページを最後まで読み、PDF を If-None-Match で確かめる
        site = FakeSite(page=page_html(news=NEWS), pdf=FakeResponse(status=304))
        old = current(pdf_check={"checked_at": int(time.time()) - 9 * 86400})
        shop, size = self.fetch(site, old)
        self.assertEqual(site.pdf_calls(), [("GET", {"If-None-Match": 'W/"a"'})])
        self.assertGreater(size, maneki.CHUNK)  # ページは最後まで読む
        self.assertEqual(shop["tables"], [["keep"]])
        self.assertGreater(shop["pdf_check"]["checked_at"], old["pdf_check"]["checked_at"])
        self.assertEqual(shop["news"], [{"date": "2026-11-20", "text": "12/29~1/3は 年末年始特別料金です"}])

    def test_changed_page_with_same_pdf_uses_etag(self):
        site = FakeSite(pdf=FakeResponse(status=304))
        shop, _ = self.fetch(site, current(modified="old"))
        self.assertEqual(site.pdf_calls(), [("GET", {"If-None-Match": 'W/"a"'})])
        self.assertEqual(shop["tables"], [["keep"]])
        self.assertEqual(shop["modified"], MODIFIED)
        self.assertEqual(shop["extract"], maneki.EXTRACT)

    def test_new_pdf_url_is_read_without_etag_and_keeps_notices(self):
        site = FakeSite()
        shop, size = self.fetch(site, current(modified="old", pdf=OTHER_PDF))
        self.assertEqual(site.pdf_calls(), [("GET", None)])
        self.assertEqual((shop["pdf"], shop["etag"], shop["length"]), (PDF, 'W/"new"', len(PDF_BODY)))
        self.assertEqual(shop["last_modified"], "Mon, 23 Nov 2026 23:11:14 GMT")
        self.assertEqual(shop["tables"], EXTRACTED["tables"])
        self.assertEqual(shop["notices"], EXTRACTED["notices"])
        self.assertEqual(shop["extract"], maneki.EXTRACT)
        self.assertNotIn("status", shop)

    def test_old_record_is_kept_until_its_turn_to_be_filled(self):
        old = {"modified": MODIFIED, "pdf": PDF, "etag": 'W/"a"', "tables": [["keep"]], "checked_at": 1}
        site = FakeSite()
        shop, _ = self.fetch(site, old)
        self.assertEqual(site.pdf_calls(), [])
        self.assertNotIn("extract", shop)
        # ページが変わっても、同じ PDF なら If-None-Match で確かめるだけ（版は古いまま、変換は読まない）
        site = FakeSite(pdf=FakeResponse(status=304))
        shop, _ = self.fetch(site, {**old, "modified": "old"})
        self.assertEqual(site.pdf_calls(), [("GET", {"If-None-Match": 'W/"a"'})])
        self.assertNotIn("extract", shop)

    def test_backfill_reads_the_same_pdf_again(self):
        old = {"modified": MODIFIED, "pdf": PDF, "etag": 'W/"a"', "tables": [["keep"]], "checked_at": 1}
        site = FakeSite()
        shop, _ = self.fetch(site, old, refresh=True)
        self.assertEqual(site.pdf_calls(), [("GET", None)])
        self.assertEqual(shop["extract"], maneki.EXTRACT)
        self.assertEqual(shop["notices"], EXTRACTED["notices"])

    def test_two_pdf_links_are_not_read(self):
        site = FakeSite(page=page_html(links=(PDF, OTHER_PDF), modified="new"))
        shop, _ = self.fetch(site, current())
        self.assertEqual(site.pdf_calls(), [])
        self.assertEqual((shop["status"], shop["pdfs"], shop["tables"]), ("several-pdfs", 2, []))

    def test_closed_shop_is_not_a_failure(self):
        site = FakeSite(page=FakeResponse(status=404))
        shop, _ = self.fetch(site, current())
        self.assertEqual((shop["status"], shop["tables"]), ("closed", []))

    def test_missing_or_broken_pdf_means_no_price(self):
        site = FakeSite(page=page_html(modified="new"), pdf=FakeResponse(status=404))
        shop, _ = self.fetch(site, current())
        self.assertEqual((shop["status"], shop["tables"]), ("pdf-missing", []))
        # PDF でない本文（HTML や S3 の XML のエラー。%%EOF はない）は、取り直さずにその日のうちに料金なし
        for body in (b"<html>not a pdf</html>", b'<?xml version="1.0"?><Error><Code>AccessDenied</Code></Error>'):
            site = FakeSite(pdf=FakeResponse(body=body))
            shop, _ = self.fetch(site, current(modified="old", pdf=OTHER_PDF))
            self.assertEqual((shop["status"], shop["tables"]), ("unreadable-pdf", []))
            self.assertEqual(len(site.pdf_calls()), 1)

    def test_communication_failures_raise_so_the_previous_record_is_kept(self):
        with self.assertRaises(maneki.Throttled):
            self.fetch(FakeSite(page=FakeResponse(status=429)), current())
        with self.assertRaises(requests.HTTPError):
            self.fetch(FakeSite(page=FakeResponse(status=500)), current())
        cut = page_html(modified="new")[:-200]
        with self.assertRaises(net.Incomplete):
            self.fetch(FakeSite(page=cut), current())
        # ページが変わっていない店の PDF の確かめ（9 日ごと）が通信の失敗なら、前回の記録を残す
        old = current(pdf_check={"checked_at": int(time.time()) - 9 * 86400})
        with self.assertRaises(requests.HTTPError):
            self.fetch(FakeSite(pdf=FakeResponse(status=500)), old)

    def test_pdf_not_fetched_after_the_page_changed_means_no_price(self):
        # ページが変わって新しい PDF を指したのに PDF を取れない（5xx、本文の欠け）。古い PDF の金額は残さない
        for pdf in (FakeResponse(status=500), FakeResponse(body=b"%PDF-1.7 cut")):
            site = FakeSite(pdf=pdf)
            old = current(modified="old", pdf=OTHER_PDF, checked_at=5, pdf_check={"checked_at": 5})
            with self.assertRaises(maneki.PdfUnfetched) as caught:
                self.fetch(site, old)
            record = caught.exception.record
            self.assertEqual((record["status"], record["tables"], record["pdf"]), ("pdf-unfetched", [], PDF))
            self.assertEqual((record["checked_at"], record["pdf_check"]), (5, {"checked_at": 5}))  # 確かめた日時は進めない
            self.assertGreater(caught.exception.size, maneki.CHUNK)
        # 本文の欠けは新しい接続で読み直してから諦める
        self.assertEqual(len(site.pdf_calls()), 1 + net.ATTEMPTS)
        # 同じ PDF でも、ページの更新日時が変わっていれば同じ扱い
        with self.assertRaises(maneki.PdfUnfetched):
            self.fetch(FakeSite(pdf=FakeResponse(status=502)), current(modified="old"))
        # 次の回は、ページが変わっていなくても最後まで読んで PDF を取り直す
        site = FakeSite()
        shop, _ = self.fetch(site, {**record, "modified": MODIFIED})
        self.assertEqual(site.pdf_calls(), [("GET", None)])
        self.assertEqual(shop["tables"], EXTRACTED["tables"])
        self.assertNotIn("status", shop)

    def test_head_is_used_when_the_server_ignores_if_none_match(self):
        fetcher = fetcher_for(FakeSite())
        logs = []
        reader = maneki.PdfReader(fetcher, log=logs.append)
        old = {"etag": 'W/"a"', "last_modified": "Tue, 01 Sep 2026 06:22:54 GMT", "length": len(PDF_BODY)}
        site = FakeSite(pdf=FakeResponse(body=PDF_BODY, headers={"ETag": 'W/"a"'}),
                        head=FakeResponse(headers={"ETag": '"a"', "Last-Modified": old["last_modified"],
                                                   "Content-Length": str(len(PDF_BODY))}))
        fetcher.session = site.session
        self.assertEqual(reader.read(PDF, old)[0], "new")
        self.assertTrue(reader.use_head)
        self.assertEqual(reader.read(PDF, old), ("same", None))
        self.assertEqual([method for method, _ in site.pdf_calls()], ["GET", "HEAD"])
        site.head = FakeResponse(headers={"ETag": '"a"', "Last-Modified": old["last_modified"], "Content-Length": "999"})
        self.assertEqual(reader.read(PDF, old)[0], "new")
        self.assertEqual(len(logs), 1)


class NoticesTest(unittest.TestCase):
    def test_store_page_news_special_hours_and_unusual_remarks(self):
        markup = ('<article><div class="storeDetail__info__specialHours__wrap inner_wrap"> <p>12/31は 22:00 閉店</p> </div>'
                  '<div class="storeDetail__wrap">' + NEWS +
                  '<div class="storeDetail__price__remarks"><ul><li>朝うた料金は、30分あたりの金額となります。</li>'
                  '<li>ワンドリンクオーダー制は、室料とは別に４５１円（税込）以上のドリンクを１品ご注文いただきます。詳しくは店舗へお問合せください。 </li>'
                  '<li>12月は特別料金となります</li></ul></div></article>')
        self.assertEqual(maneki.store_notices(markup), [
            {"date": "2026-11-20", "text": "12/29~1/3は 年末年始特別料金です"},
            {"date": "", "kind": "special-hours", "text": "12/31は 22:00 閉店"},
            {"date": "", "text": "12月は特別料金となります"},
        ])
        # 欄の作りが変わって読めないときは、日付のない特別営業時間の文として残す（変換は読まない）
        changed = '<div class="storeDetail__info__specialHours__body"><p>12/31は 22:00 閉店</p></div>'
        self.assertEqual(maneki.store_notices(changed), [{"date": "", "kind": "special-hours", "text": "特別営業時間の欄を読めません"}])
        self.assertEqual(maneki.store_notices(NEWS), [{"date": "2026-11-20", "text": "12/29~1/3は 年末年始特別料金です"}])
        self.assertEqual(maneki.pdf_links(f'<a href="{PDF}"></a><a href="{PDF}"></a>'), [PDF])


@unittest.skipIf(maneki.pypdfium2 is None, "pypdfium2 がない")
class ExtractTest(unittest.TestCase):
    """その場で作った 2 ページの PDF（文字のページと、大きな画像だけのページ）から取り出す。"""

    @staticmethod
    def make_pdf() -> bytes:
        pdfium, raw = maneki.pypdfium2, maneki.pdfium_c
        document = pdfium.PdfDocument.new()
        page = document.new_page(595, 842)
        y = 800
        for word in ("TABLE 30", "Special price from 12/29"):
            item = raw.FPDFPageObj_NewTextObj(document, b"Helvetica", 12.0)
            text = ctypes.create_string_buffer((word + "\0").encode("utf-16-le"))
            raw.FPDFText_SetText(item, ctypes.cast(text, ctypes.POINTER(raw.FPDF_WCHAR)))
            raw.FPDFPageObj_Transform(item, 1, 0, 0, 1, 50, y)
            raw.FPDFPage_InsertObject(page, item)
            y -= 20
        raw.FPDFPage_GenerateContent(page)
        flyer = document.new_page(595, 842)
        bitmap = pdfium.PdfBitmap.new_native(60, 80, raw.FPDFBitmap_BGR)
        bitmap.fill_rect((40, 40, 40, 255), 0, 0, 60, 80)
        bitmap.fill_rect((250, 250, 250, 255), 0, 0, 30, 40)
        image = pdfium.PdfImage.new(document)
        image.set_bitmap(bitmap)
        image.set_matrix(pdfium.PdfMatrix().scale(500, 760).translate(40, 40))
        flyer.insert_obj(image)
        flyer.gen_content()
        out = io.BytesIO()
        document.save(out)
        return out.getvalue()

    def test_pages_texts_and_looks(self):
        with mock.patch.object(maneki, "is_price_table", lambda runs: any(run[0] == "TABLE" for run in runs)):
            result = maneki.extract(self.make_pdf())
        self.assertEqual([token[0] for token in result["tables"][0]], ["TABLE", "30", "Special", "price", "from", "12/29"])
        self.assertEqual(result["notices"], [{"page": 1, "table": True, "text": "TABLE 30 Special price from 12/29"}])
        table, flyer = result["pages"]
        self.assertTrue(table["table"])
        self.assertRegex(table["look"], r"^[0-9a-f]{16}$")
        self.assertEqual((flyer["table"], flyer["chars"], flyer["images"]), (False, 0, 1))
        self.assertGreater(flyer["image_area"], maneki.VISUAL_AREA)
        self.assertRegex(flyer["look"], r"^[0-9a-f]{16}$")


class TokensTest(unittest.TestCase):
    def test_price_table_needs_label_and_member(self):
        self.assertTrue(maneki.is_price_table([["３０分", 0, 0, 0, 0], ["会員", 0, 0, 0, 0]]))
        self.assertFalse(maneki.is_price_table([["30分毎", 0, 0, 0, 0], ["会員", 0, 0, 0, 0]]))
        self.assertFalse(maneki.is_price_table([["30分", 0, 0, 0, 0]]))

    def test_keep_tokens_drops_options_but_keeps_notes_in_drawn_order(self):
        runs = [
            ["30分", 1, 10, 2, 11],
            ["円", 3, 10, 4, 11],
            ["オプション", 1, 50, 2, 51],
            ["660", 1, 60, 2, 61],
            ["※ご利用は1時間から", 1, 90, 2, 91],
            ["※お1人様", 1, 95, 2, 96],
            ["会員", 1, 5, 2, 6],
        ]
        self.assertEqual([token[0] for token in maneki.keep_tokens(runs)], ["30分", "※ご利用は1時間から", "※お1人様", "会員"])


class AcceptTest(unittest.TestCase):
    def test_big_drop_keeps_previous(self):
        previous = {str(i): {"tables": [[1]]} for i in range(10)}
        self.assertFalse(maneki.accept(previous, {str(i): {"tables": [[1]]} for i in range(7)}))
        self.assertTrue(maneki.accept(previous, {str(i): {"tables": [[1]]} for i in range(8)}))
        # 理由の分かる消え方（PDF のリンクが 2 つ、PDF を取れなかった、読み直して料金表がなかった）は数えない
        explained = {str(i): {"tables": [[1]]} for i in range(5)}
        explained.update({str(i): {"status": "several-pdfs", "tables": []} for i in range(5, 8)})
        explained.update({"8": {"status": "pdf-unfetched", "tables": []}, "9": {"extract": maneki.EXTRACT, "pages": [], "tables": []}})
        self.assertTrue(maneki.accept(previous, explained))
        self.assertFalse(maneki.accept(previous, {**explained, "9": {"tables": []}, "8": {"tables": []}, "7": {}}))


class SinceTest(unittest.TestCase):
    """画像のページに、その店にその画像が載り始めた日時（since）を付ける"""

    flyer = {"table": False, "chars": 0, "paths": 2, "images": 2, "image_area": 0.88, "look": "78f9717b71512533"}

    def test_new_image_starts_at_the_upload_time_and_known_image_keeps_its_since(self):
        now = 1_900_000_000
        uploaded = maneki.uploaded_at("Mon, 23 Nov 2026 23:11:14 GMT", now)
        self.assertEqual(uploaded, 1795475474)  # 2026-11-23 23:11:14 UTC
        self.assertEqual(maneki.uploaded_at("", now), now)  # 読めなければ今
        self.assertEqual(maneki.uploaded_at("Mon, 23 Nov 2099 23:11:14 GMT", now), now)  # 未来は今
        table = {"table": True, "chars": 900, "paths": 250, "images": 4, "image_area": 0.01, "look": "8e8eccce8e8e480e"}
        pages = maneki.with_since([dict(table), dict(self.flyer)], {}, uploaded)
        self.assertNotIn("since", pages[0])
        self.assertEqual(pages[1]["since"], uploaded)
        # 前回の PDF に同じ見た目（4 ビット以内）の画像があれば、その日時を引き継ぐ
        old = {"extract": maneki.EXTRACT, "pages": [{**self.flyer, "look": "78f9717b71512531", "since": 1_600_000_000}]}
        self.assertEqual(maneki.with_since([dict(self.flyer)], old, uploaded)[0]["since"], 1_600_000_000)
        # 見た目の違う新しい画像は、PDF を上げた日時から
        self.assertEqual(maneki.with_since([{**self.flyer, "look": "f00ff00ff00ff00f"}], old, uploaded)[0]["since"], uploaded)
        # 古い版の記録の日時は使わない
        self.assertEqual(maneki.with_since([dict(self.flyer)], {**old, "extract": 1}, uploaded)[0]["since"], uploaded)

    def test_backfill_takes_the_oldest_old_records_with_tables_first(self):
        pages = {f"mn-{i}": "u" for i in range(80)}
        shops = {f"mn-{i}": {"tables": [["30分"]], "checked_at": 1000 + i} for i in range(80)}
        shops["mn-0"]["extract"] = maneki.EXTRACT
        shops["mn-60"]["tables"] = shops["mn-1"]["tables"] = []  # 文字のない PDF の店は後回し
        chosen = maneki.backfill(pages, shops, {"files": {}, "tried": {}, "lists": {}})
        self.assertEqual(len(chosen), maneki.BACKFILL)
        self.assertEqual(chosen, {f"mn-{i}" for i in range(2, maneki.BACKFILL + 2)})


class UpdateTest(Base):
    """1 回の更新。元データは一時フォルダに置き、巡回の計画と seen.json は差し替える"""

    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.data = Path(folder.name) / "maneki_prices.json"
        self.previous = {f"mn-{i}": current(tables=[[f"old-{i}"]]) for i in range(5)}
        self.data.write_text(json.dumps({"schema_version": 1, "updated_at": 7, "shops": self.previous}), encoding="utf-8")
        for patcher in (mock.patch.object(maneki, "DATA_FILE", self.data),
                        mock.patch.object(maneki, "shop_pages", return_value={key: f"https://x/{key}" for key in self.previous}),
                        mock.patch.object(maneki.seen, "load", return_value={"files": {}, "tried": {}, "lists": {}}),
                        mock.patch.object(maneki.crawl_plan, "due", return_value=list(self.previous))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def saved(self):
        return json.loads(self.data.read_text(encoding="utf-8"))

    def test_deadline_saves_what_was_read_and_ends_normally(self):
        def fetch(fetcher, reader, url, old, refresh=False):
            if url.endswith("mn-2"):
                raise net.OutOfTime("締め切り")
            return {**old, "tables": [["new"]]}, 10

        with mock.patch.object(maneki, "fetch_shop", side_effect=fetch):
            self.assertTrue(maneki.update(log=lambda line: None))
        saved = self.saved()
        self.assertEqual(saved["shops"]["mn-1"]["tables"], [["new"]])
        self.assertEqual(saved["shops"]["mn-3"]["tables"], [["old-3"]])
        self.assertEqual(saved["updated_at"], 7)  # 全店を確かめていないので進めない

    def test_one_broken_shop_keeps_its_previous_record_and_others_are_saved(self):
        def fetch(fetcher, reader, url, old, refresh=False):
            if url.endswith("mn-1"):
                raise KeyError("想定外の形")
            return {**old, "tables": [["new"]]}, 10

        with mock.patch.object(maneki, "fetch_shop", side_effect=fetch):
            self.assertFalse(maneki.update(log=lambda line: None))
        saved = self.saved()["shops"]
        self.assertEqual(saved["mn-1"]["tables"], [["old-1"]])
        self.assertEqual(saved["mn-4"]["tables"], [["new"]])

    def test_pdf_not_fetched_is_saved_without_price_and_counted_as_a_failure(self):
        def fetch(fetcher, reader, url, old, refresh=False):
            if url.endswith("mn-1"):
                raise maneki.PdfUnfetched({**old, "status": "pdf-unfetched", "tables": []}, 10, requests.HTTPError("500"))
            return {**old, "tables": [["new"]]}, 10

        with mock.patch.object(maneki, "fetch_shop", side_effect=fetch):
            self.assertFalse(maneki.update(log=lambda line: None))
        saved = self.saved()
        self.assertEqual((saved["shops"]["mn-1"]["status"], saved["shops"]["mn-1"]["tables"]), ("pdf-unfetched", []))
        self.assertEqual(saved["shops"]["mn-2"]["tables"], [["new"]])
        self.assertEqual(saved["updated_at"], 7)

    def test_throttling_stops_and_keeps_what_was_read(self):
        def fetch(fetcher, reader, url, old, refresh=False):
            if url.endswith("mn-3"):
                raise maneki.Throttled("HTTP 429")
            return {**old, "tables": [["new"]]}, 10

        with mock.patch.object(maneki, "fetch_shop", side_effect=fetch):
            self.assertFalse(maneki.update(log=lambda line: None))
        saved = self.saved()["shops"]
        self.assertEqual((saved["mn-2"]["tables"], saved["mn-4"]["tables"]), ([["new"]], [["old-4"]]))


class ShopListTest(unittest.TestCase):
    """店一覧の「もっと見る」の POST は、失敗したページから読み直して続ける"""

    def setUp(self):
        patcher = mock.patch.object(update_module.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def session(self, posts):
        session = mock.Mock()
        top = FakeResponse(body=b'<script>var LOC_AJAX = {"nonce":"n1"};</script>')
        top.text, top.encoding = top.content.decode(), "utf-8"
        session.get.return_value = top
        pages = []

        def post(url, data=None, timeout=None):
            pages.append(data["page"])
            result = posts.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        session.post.side_effect = post
        return session, pages

    @staticmethod
    def page(urls, more, next_page=None):
        html = "".join(f'<a href="https://www.karaokemanekineko.jp/locations/a/b/{name}-store/">' for name in urls)
        data = {"html": html, "has_more": more, **({"next_page": next_page} if next_page else {})}
        response = FakeResponse(body=json.dumps({"data": data}).encode())
        response.json = lambda: {"data": data}
        return response

    def test_failed_page_is_read_again_and_listing_continues(self):
        session, pages = self.session([self.page(["a"], True, 2), requests.ReadTimeout("slow"), FakeResponse(status=502),
                                       self.page(["b"], False)])
        urls = update_module.maneki_urls(session, 0.2)
        self.assertEqual(pages, [1, 2, 2, 2])
        self.assertEqual([url.split("/")[-2] for url in urls], ["a-store", "b-store"])

    def test_listing_fails_when_a_page_cannot_be_read(self):
        session, _ = self.session([self.page(["a"], True, 2)] + [requests.ReadTimeout("slow")] * 3)
        with self.assertRaises(requests.ReadTimeout):
            update_module.maneki_urls(session, 0.2)
        session, _ = self.session([FakeResponse(status=429)])
        with self.assertRaises(update_module.FetchStopped):
            update_module.maneki_urls(session, 0.2)


if __name__ == "__main__":
    unittest.main()
