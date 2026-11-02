import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import update_maneki_prices as maneki

PAGE = '<meta property="article:modified_time" content="2026-10-16T11:10:02+00:00" />' + " " * 9000
PDF = "https://www.karaokemanekineko.jp/wp-content/uploads/2025/11/16200950/623.pdf"


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None):
        self.status_code = status
        self.content = body
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise maneki.requests.HTTPError(str(self.status_code))

    def iter_content(self, size):
        for start in range(0, len(self.content), size):
            yield self.content[start:start + size]


class FakeSession:
    """読まれた量と PDF の取得を数える。"""

    def __init__(self, page=PAGE + f'<a href="{PDF}" class="storeDetail__price__button__link">', pdf=None):
        self.page = page.encode()
        self.pdf = pdf or FakeResponse(body=b"%PDF-1.7", headers={"ETag": '"new"'})
        self.pdf_calls = []

    def get(self, url, timeout=0, stream=False, headers=None):
        if url.endswith(".pdf"):
            self.pdf_calls.append(headers)
            return self.pdf
        return FakeResponse(body=self.page)


class FetchShopTest(unittest.TestCase):
    def setUp(self):
        for patcher in (mock.patch.object(maneki.time, "sleep"), mock.patch.object(maneki, "tables_of", return_value=[[["30分", 1, 2, 3, 4]]])):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_same_modified_time_stops_early_and_skips_pdf(self):
        session = FakeSession()
        old = {"modified": "2026-10-16T11:10:02+00:00", "pdf": PDF, "etag": '"a"', "tables": [[]], "checked_at": 1}
        shop, size = maneki.fetch_shop(session, "https://x/", old)
        self.assertEqual(session.pdf_calls, [])
        self.assertLessEqual(size, maneki.CHUNK)
        self.assertEqual(shop["pdf"], PDF)
        self.assertGreater(shop["checked_at"], 1)

    def test_changed_time_with_same_pdf_uses_etag(self):
        session = FakeSession(pdf=FakeResponse(status=304))
        old = {"modified": "old", "pdf": PDF, "etag": '"a"', "tables": [["keep"]], "checked_at": 1}
        shop, _ = maneki.fetch_shop(session, "https://x/", old)
        self.assertEqual(session.pdf_calls, [{"If-None-Match": '"a"'}])
        self.assertEqual(shop["tables"], [["keep"]])
        self.assertEqual(shop["modified"], "2026-10-16T11:10:02+00:00")

    def test_new_pdf_url_is_read_without_etag(self):
        session = FakeSession()
        old = {"modified": "old", "pdf": "https://www.karaokemanekineko.jp/wp-content/uploads/old.pdf", "etag": '"a"', "tables": [], "checked_at": 1}
        shop, _ = maneki.fetch_shop(session, "https://x/", old)
        self.assertEqual(session.pdf_calls, [{}])
        self.assertEqual(shop["pdf"], PDF)
        self.assertEqual(shop["etag"], '"new"')
        self.assertEqual(shop["tables"], [[["30分", 1, 2, 3, 4]]])

    def test_not_a_pdf_is_rejected(self):
        session = FakeSession(pdf=FakeResponse(body=b"<html>"))
        with self.assertRaises(ValueError):
            maneki.fetch_shop(session, "https://x/", {})

    def test_throttled_page_raises(self):
        session = FakeSession()
        session.get = lambda *args, **kwargs: FakeResponse(status=429)
        with self.assertRaises(maneki.Throttled):
            maneki.fetch_shop(session, "https://x/", {})


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


if __name__ == "__main__":
    unittest.main()
