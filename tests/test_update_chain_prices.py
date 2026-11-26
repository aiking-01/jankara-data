"""実行: python -m unittest discover tests"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import net  # noqa: E402
import update_chain_prices as u  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "bigecho"

PAGE = """
<html><head><script type="application/ld+json">{"@type": "LocalBusiness", "url": "https://big-echo.jp/?p=1"}</script></head><body>
<dl><dt>営業時間</dt><dd><div>月曜日  9:00～2:00</div><div>日曜・祝祭日  9:00～2:00</div></dd></dl>
<div id="price">
  <h3>30分料金（税込）</h3>
  <h4>OPEN～18：00</h4>
  <table>
    <tr><th colspan="3">1ドリンクオーダー制</th></tr>
    <tr><th></th><th>月～金</th><th>土・日・祝日</th></tr>
    <tr><th>ひとりカラオケ<br>(会員の場合)</th><td>200円</td><td>300円</td></tr>
    <tr><td colspan="3">※注意書き</td></tr>
  </table>
  <h3>フリータイム</h3>
  <h4>OPEN～20：00</h4>
  <table><tr><th></th><th>全日</th></tr><tr><th>会員</th><td>900円</td></tr></table>
</div>
</body></html>
"""
# 表を外して特別料金の見出しだけにした店舗ページ（その店の JSON-LD はある）
SPECIAL = """
<html><head><script type="application/ld+json">{"@type": "LocalBusiness", "url": "https://big-echo.jp/?p=1"}</script></head><body>
<dl><dt>店舗からのお知らせ</dt><dd>11/27（金）～1/3（日）期間中、年末特別料金での営業となります。<br>詳しくは店舗まで</dd></dl>
<div id="price"><h3>※12/1～1/5は特別料金となります。料金は店舗へ直接お問い合わせください。</h3></div>
</body></html>
"""
TOP = "<html><body>トップページ</body></html>"


def page(stem: str) -> str:
    return (FIXTURES / f"{stem}.html").read_text(encoding="utf-8")


class ParsePage(unittest.TestCase):
    def test_reads_every_table_and_hours(self):
        parsed = u.parse_page(PAGE)
        self.assertEqual(parsed["hours"], "月曜日 9:00～2:00日曜・祝祭日 9:00～2:00")
        self.assertEqual(len(parsed["sections"]), 2)
        self.assertEqual(parsed["sections"][1]["heading"], "フリータイム")
        section = parsed["sections"][0]
        self.assertEqual(section["time"], "OPEN～18：00")
        self.assertEqual(section["title"], "1ドリンクオーダー制")
        self.assertEqual(section["note"], "※注意書き")
        self.assertEqual(section["header"], ["", "月～金", "土・日・祝日"])
        self.assertEqual(section["rows"], [["ひとりカラオケ(会員の場合)", "200円", "300円"]])
        self.assertEqual(parsed["notice"], "")
        self.assertEqual(parsed["price_notes"], [])

    def test_page_without_prices(self):
        self.assertIsNone(u.parse_page("<html><body>メンテナンス中</body></html>"))

    def test_reads_notice_of_price_change(self):
        parsed = u.parse_page(page("be-1343"))
        self.assertEqual(len(parsed["sections"]), 8)
        self.assertIn("2026年12月1日より年末年始期間中、一部の料金を改定させていただきます。", parsed["notice"].split("\n"))

    def test_keeps_headings_without_tables(self):
        parsed = u.parse_page(page("be-0473"))
        self.assertEqual(parsed["sections"], [])
        self.assertEqual(parsed["price_notes"], ["※12/1～1/5は特別料金となります。料金は店舗へ直接お問い合わせください。"])
        parsed = u.parse_page(page("be-1198"))
        self.assertEqual(parsed["sections"], [])
        self.assertEqual(parsed["price_notes"], ["◆３０分料金", "◆３時間パック・フリータイム"])
        self.assertTrue(parsed["notice"].startswith("★11/27（金）～1/3（日）の期間は一部料金を変更して営業致します。"))

    def test_empty_price_area_keeps_notice(self):
        parsed = u.parse_page(page("be-1135"))
        self.assertEqual(parsed["sections"], [])
        self.assertEqual(parsed["price_notes"], [])
        self.assertIn("年末特別料金", parsed["notice"])

    def test_reads_shop_url_from_json_ld(self):
        from bs4 import BeautifulSoup
        self.assertEqual(u.shop_url(BeautifulSoup(page("be-1323-new"), "html.parser")), "https://big-echo.jp/?p=22446")
        self.assertEqual(u.shop_url(BeautifulSoup(page("be-1323-old"), "html.parser")), "")
        self.assertEqual(u.post_id("https://big-echo.jp/?p=22446"), "22446")
        self.assertEqual(u.post_id("https://big-echo.jp/shop_info/a"), "")


def response(text="", payload=None, status=200, url="https://big-echo.jp/?p=1"):
    result = mock.Mock(status_code=status, text=text, url=url, content=text.encode("utf-8"), headers={})
    result.json.return_value = payload
    result.raise_for_status.side_effect = None if status < 400 else u.requests.HTTPError(str(status))
    return result


class Update(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for target, attribute, value in [
            (u, "DATA_FILE", Path(folder.name) / "chain_prices.json"),
            (u, "DELAY", 0),
            (u, "FETCH_PAUSE", 0),
        ]:
            patcher = mock.patch.object(target, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # 巡回の記録（source/seen.json）は読まず、書かない
        empty = {"schema_version": 1, "files": {}, "tried": {}, "lists": {}}
        for patcher in (mock.patch.object(u.crawl_plan.seen, "load", side_effect=lambda path=None: json.loads(json.dumps(empty))),
                        mock.patch.object(u.crawl_plan, "recording", return_value=False),
                        mock.patch.dict(os.environ, {"CRAWL_DEADLINE": "", "CRAWL_ALL": "", "HTTPS_PROXY": "", "HTTP_PROXY": "",
                                                     "https_proxy": "", "http_proxy": ""})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def stored(self, shops, updated_at=1):
        u.save(shops, updated_at)

    def read(self):
        return json.loads(u.DATA_FILE.read_text(encoding="utf-8"))

    def run_update(self, ids, get):
        session = mock.Mock(headers={})
        session.get.side_effect = lambda url, **kwargs: get(url)
        with mock.patch.object(u, "shop_ids", return_value=ids), mock.patch.object(u.requests, "Session", return_value=session):
            ok = u.update(log=lambda line: None)
        return ok, session

    @staticmethod
    def urls(session):
        return [call.args[0] for call in session.get.call_args_list]

    def test_fetches_and_remembers_page_url(self):
        def get(url):
            if "api/point" in url:
                return response(payload={"短縮ＵＲＬ": "https://big-echo.jp/?p=1"})
            return response(PAGE, url="https://big-echo.jp/shop_info/a")
        ok, session = self.run_update(["be-0001"], get)
        self.assertTrue(ok)
        shop = self.read()["shops"]["be-0001"]
        self.assertEqual(shop["page"], "https://big-echo.jp/shop_info/a")
        self.assertEqual(shop["short"], "https://big-echo.jp/?p=1")
        self.assertEqual(len(shop["sections"]), 2)
        self.assertEqual(shop["scope"], "all")
        self.assertGreater(self.read()["updated_at"], 1)
        # 次回は保存した URL を使い、API を呼ばない
        shops = self.read()["shops"]
        shops["be-0001"]["checked_at"] = 0
        self.stored(shops)
        _, session = self.run_update(["be-0001"], lambda url: response(PAGE, url=url))
        self.assertEqual(self.urls(session), ["https://big-echo.jp/shop_info/a"])

    def test_remembers_redirect_target_on_https(self):
        def get(url):
            if "api/point" in url:
                return response(payload={"短縮ＵＲＬ": "https://big-echo.jp/?p=1"})
            return response(PAGE, url="http://big-echo.jp/shop_info/a")
        self.run_update(["be-0001"], get)
        self.assertEqual(self.read()["shops"]["be-0001"]["page"], "https://big-echo.jp/shop_info/a")

    def test_redirect_outside_official_site_is_not_remembered(self):
        known = "https://big-echo.jp/shop_info/a"
        self.stored({"be-0001": {"hours": "", "sections": [{}], "page": known, "checked_at": 0}})
        ok, _ = self.run_update(["be-0001"], lambda url: response(PAGE, url="https://example.test/"))
        self.assertFalse(ok)
        self.assertEqual(self.read()["shops"]["be-0001"]["page"], known)
        self.assertEqual(self.read()["shops"]["be-0001"]["sections"], [{}])

    def test_page_without_json_ld_does_not_save_an_empty_short_url(self):
        known = "https://big-echo.jp/shop_info/a"
        self.stored({"be-0001": {"hours": "", "sections": [{}], "page": known, "checked_at": 0}})
        without = PAGE.replace('"url": "https://big-echo.jp/?p=1"', '"name": "a"')
        ok, _ = self.run_update(["be-0001"], lambda url: response(without, url=known))
        self.assertTrue(ok)
        shop = self.read()["shops"]["be-0001"]
        self.assertEqual(len(shop["sections"]), 2)
        self.assertNotIn("short", shop)

    def test_temporary_error_is_retried_once(self):
        known = "https://big-echo.jp/shop_info/a"
        self.stored({"be-0001": {"hours": "", "sections": [{}], "page": known, "checked_at": 0}})
        replies = [response(status=502), response(PAGE, url=known)]
        ok, session = self.run_update(["be-0001"], lambda url: replies.pop(0))
        self.assertTrue(ok)
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(len(self.read()["shops"]["be-0001"]["sections"]), 2)

    def test_stops_fetching_when_throttled(self):
        shops = {f"be-{i:04}": {"hours": "h", "sections": [{"time": "old"}], "page": "https://big-echo.jp/?p=1", "checked_at": 0} for i in range(50)}
        self.stored(shops, updated_at=5)
        ok, session = self.run_update(list(shops), lambda url: response(status=429))
        self.assertFalse(ok)
        self.assertLess(session.get.call_count, 50)
        self.assertEqual(self.read()["shops"]["be-0030"]["sections"], [{"time": "old"}])
        self.assertEqual(self.read()["updated_at"], 5)

    def test_recent_shops_are_skipped(self):
        self.stored({"be-0001": {"hours": "", "sections": [{}], "page": "https://big-echo.jp/?p=1", "scope": "all", "checked_at": int(time.time())}})
        _, session = self.run_update(["be-0001"], lambda url: response(PAGE))
        session.get.assert_not_called()

    def test_shops_saved_with_30min_tables_only_are_fetched_again(self):
        self.stored({"be-0001": {"hours": "", "sections": [{}], "page": "https://big-echo.jp/?p=1", "checked_at": int(time.time())}})
        _, session = self.run_update(["be-0001"], lambda url: response(PAGE))
        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(len(self.read()["shops"]["be-0001"]["sections"]), 2)

    def test_failure_keeps_previous_and_does_not_advance_timestamp(self):
        old = {"hours": "h", "sections": [{"time": "old"}], "page": "https://big-echo.jp/?p=1", "checked_at": 0}
        self.stored({"be-0001": old}, updated_at=5)
        ok, _ = self.run_update(["be-0001"], lambda url: response(status=500))
        self.assertFalse(ok)
        self.assertEqual(self.read()["shops"]["be-0001"]["sections"], [{"time": "old"}])
        self.assertEqual(self.read()["shops"]["be-0001"]["checked_at"], 0)
        self.assertEqual(self.read()["updated_at"], 5)

    def test_truncated_page_is_a_network_failure_and_keeps_previous(self):
        old = {"hours": "h", "sections": [{"time": "old"}], "page": "https://big-echo.jp/?p=1", "checked_at": 0}
        self.stored({"be-0001": old}, updated_at=5)
        ok, _ = self.run_update(["be-0001"], lambda url: response(PAGE[: len(PAGE) // 2]))
        self.assertFalse(ok)
        self.assertEqual(self.read()["shops"]["be-0001"]["sections"], [{"time": "old"}])
        self.assertEqual(self.read()["updated_at"], 5)

    def test_timeout_keeps_previous(self):
        old = {"hours": "h", "sections": [{"time": "old"}], "page": "https://big-echo.jp/?p=1", "checked_at": 0}
        self.stored({"be-0001": old}, updated_at=5)

        def get(url):
            raise u.requests.Timeout("timeout")
        ok, _ = self.run_update(["be-0001"], get)
        self.assertFalse(ok)
        self.assertEqual(self.read()["shops"]["be-0001"], old)

    def test_shop_page_without_tables_drops_previous_values(self):
        # 店舗ページだと確かめられたのに表がない店（年末の特別料金で表を外した）は、表の数に関係なく前回の金額を残さない
        shops = {f"be-{i:04}": {"hours": "h", "sections": [{"time": "old"}], "page": "https://big-echo.jp/?p=1", "checked_at": 0} for i in range(10)}
        self.stored(shops, updated_at=5)
        ok, _ = self.run_update(list(shops), lambda url: response(SPECIAL))
        self.assertTrue(ok)
        for shop in self.read()["shops"].values():
            self.assertEqual(shop["sections"], [])
            self.assertEqual(shop["price_notes"], ["※12/1～1/5は特別料金となります。料金は店舗へ直接お問い合わせください。"])
            self.assertIn("年末特別料金", shop["notice"])
            self.assertGreater(shop["checked_at"], 0)
        self.assertGreater(self.read()["updated_at"], 5)

    def test_empty_price_area_on_shop_page_drops_previous_values(self):
        empty = '<html><head><script type="application/ld+json">{"@type": "LocalBusiness", "url": "https://big-echo.jp/?p=1"}</script></head><body><div id="price"></div></body></html>'
        self.stored({"be-0001": {"hours": "h", "sections": [{"time": "old"}], "page": "https://big-echo.jp/?p=1", "checked_at": 0}})
        ok, _ = self.run_update(["be-0001"], lambda url: response(empty))
        self.assertTrue(ok)
        self.assertEqual(self.read()["shops"]["be-0001"]["sections"], [])

    def test_shop_page_whose_price_area_is_gone_drops_previous_values(self):
        # 料金欄ごとなくなっても、その店の JSON-LD がある店舗ページなら料金なしにする
        gone = '<html><head><script type="application/ld+json">{"@type": "LocalBusiness", "url": "https://big-echo.jp/?p=1"}</script></head><body>店舗の案内</body></html>'
        self.stored({"be-0001": {"hours": "h", "sections": [{"time": "old"}], "page": "https://big-echo.jp/?p=1", "short": "https://big-echo.jp/?p=1", "checked_at": 0}})
        ok, _ = self.run_update(["be-0001"], lambda url: response(gone))
        self.assertTrue(ok)
        self.assertEqual(self.read()["shops"]["be-0001"]["sections"], [])

    def test_page_that_cannot_be_confirmed_keeps_previous_values(self):
        # 料金欄も店の情報もないページ（サイトの作りの変更、メンテナンス）は、店舗 API で調べ直しても同じなら前回を残す
        old = {"hours": "h", "sections": [{"time": "old"}], "page": "https://big-echo.jp/shop_info/a", "checked_at": 0}
        self.stored({"be-0001": old}, updated_at=5)

        def get(url):
            if "api/point" in url:
                return response(payload={"短縮ＵＲＬ": "https://big-echo.jp/?p=1"})
            return response(TOP, url="https://big-echo.jp/")
        ok, session = self.run_update(["be-0001"], get)
        self.assertFalse(ok)
        self.assertEqual(self.read()["shops"]["be-0001"], old)
        self.assertEqual(self.read()["updated_at"], 5)
        self.assertEqual(len([url for url in self.urls(session) if "api/point" in url]), 1)

    def test_renamed_shop_is_looked_up_again(self):
        # 店名が変わって保存した URL がトップへ転送される（be-1323 の実ページ）。店舗 API で新しい URL を調べる
        old_url = "https://big-echo.jp/shop_info/%E9%95%B7%E5%B4%8E%E6%98%A5%E9%9B%A8"
        new_url = "https://big-echo.jp/shop_info/%E9%95%B7%E5%B4%8E%E9%8A%85%E5%BA%A7"
        self.stored({"be-9323": {"hours": "h", "sections": [{"time": "old"}], "page": old_url, "checked_at": 0}})

        def get(url):
            if url == old_url:
                return response(page("be-1323-old"), url="https://big-echo.jp/")
            if url.endswith("api/point/9323"):
                return response(payload={"name": "長崎銅座店", "短縮ＵＲＬ": "https://big-echo.jp/?p=22446"})
            if url == "https://big-echo.jp/?p=22446":
                return response(page("be-1323-new"), url=new_url)
            raise AssertionError(url)
        ok, session = self.run_update(["be-9323"], get)
        self.assertTrue(ok)
        shop = self.read()["shops"]["be-9323"]
        self.assertEqual(shop["page"], new_url)
        self.assertEqual(shop["short"], "https://big-echo.jp/?p=22446")
        self.assertEqual(len(shop["sections"]), 14)
        self.assertIn("特別料金期間", shop["notice"])
        self.assertEqual(self.urls(session), [old_url, "https://shop.big-echo.jp/api/point/9323", "https://big-echo.jp/?p=22446"])

    def test_page_of_another_shop_is_not_saved(self):
        # 保存した URL の先が別の店のページになった（店名の付け替え）。JSON-LD の店の番号で見分ける
        known = "https://big-echo.jp/shop_info/a"
        old = {"hours": "h", "sections": [{"time": "old"}], "page": known, "short": "https://big-echo.jp/?p=2", "checked_at": 0}
        self.stored({"be-0002": old}, updated_at=5)

        def get(url):
            if "api/point" in url:
                return response(payload={"短縮ＵＲＬ": "https://big-echo.jp/?p=2"})
            return response(PAGE, url=known)  # どの URL でも ?p=1 の店のページが返る
        ok, _ = self.run_update(["be-0002"], get)
        self.assertFalse(ok)
        self.assertEqual(self.read()["shops"]["be-0002"], old)

    def test_looking_up_again_is_limited_per_run(self):
        shops = {f"be-{i:04}": {"hours": "h", "sections": [{"time": "old"}], "page": f"https://big-echo.jp/shop_info/{i}", "checked_at": 0} for i in range(10)}
        self.stored(shops, updated_at=5)

        def get(url):
            if "api/point" in url:
                return response(payload={"短縮ＵＲＬ": "https://big-echo.jp/?p=1"})
            return response(TOP, url="https://big-echo.jp/")
        ok, session = self.run_update(list(shops), get)
        self.assertFalse(ok)
        self.assertEqual(len([url for url in self.urls(session) if "api/point" in url]), u.RESOLVE_LIMIT)
        self.assertEqual(self.read()["shops"], shops)

    def test_shop_missing_from_api_has_no_prices(self):
        known = "https://big-echo.jp/shop_info/a"
        self.stored({"be-0001": {"hours": "h", "sections": [{"time": "old"}], "page": known, "checked_at": 0}})

        def get(url):
            if "api/point" in url:
                return response(status=404)
            return response(TOP, url="https://big-echo.jp/")
        ok, _ = self.run_update(["be-0001"], get)
        self.assertTrue(ok)
        shop = self.read()["shops"]["be-0001"]
        self.assertEqual(shop["sections"], [])
        self.assertEqual(shop["hours"], "")
        self.assertEqual(shop["page"], known)

    def test_deadline_stops_quietly_and_keeps_what_was_fetched(self):
        shops = {f"be-{i:04}": {"hours": "h", "sections": [{"time": "old"}], "page": "https://big-echo.jp/?p=1", "checked_at": 0} for i in range(3)}
        self.stored(shops, updated_at=5)
        calls = []

        def get(url):
            calls.append(url)
            if len(calls) > 1:
                os.environ["CRAWL_DEADLINE"] = str(int(time.time()) - 1)
            return response(PAGE)
        with mock.patch.object(u, "PARALLEL", 1):
            ok, _ = self.run_update(list(shops), get)
        self.assertTrue(ok)
        saved = self.read()
        self.assertEqual(len(saved["shops"]["be-0000"]["sections"]), 2)
        self.assertEqual(saved["shops"]["be-0002"]["sections"], [{"time": "old"}])
        self.assertEqual(saved["updated_at"], 5)

    def test_closed_shop_is_removed(self):
        recent = int(time.time())
        ids = [f"be-{i:04}" for i in range(10)]
        self.stored({shop_id: {"hours": "", "sections": [{}], "scope": "all", "checked_at": recent} for shop_id in ids})
        self.run_update(ids[:9], lambda url: response(PAGE))
        self.assertEqual(list(self.read()["shops"]), ids[:9])

    def test_unexpected_page_host_is_rejected(self):
        ok, _ = self.run_update(["be-0001"], lambda url: response(payload={"短縮ＵＲＬ": "https://example.test/"}))
        self.assertFalse(ok)

    def test_every_request_waits_including_retries(self):
        # 読み直し（net.Fetcher の中の取り直し）も含めて、すべてのリクエストの前に間をあける
        self.stored({"be-0001": {"hours": "h", "sections": [{}], "page": "https://big-echo.jp/?p=1", "checked_at": 0}})
        replies = [u.requests.Timeout("timeout"), response(PAGE)]

        def get(url):
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        with mock.patch.object(u.Pacer, "wait", autospec=True) as wait:
            ok, session = self.run_update(["be-0001"], get)
        self.assertTrue(ok)
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(wait.call_count, 2)


class Pacing(unittest.TestCase):
    def test_requests_from_all_threads_are_spaced_by_delay(self):
        self.assertGreaterEqual(u.DELAY, 1.0)
        sleeps = []
        with mock.patch.object(u.time, "monotonic", return_value=100.0), \
                mock.patch.object(u.time, "sleep", side_effect=sleeps.append):
            pacer = u.Pacer()
            for _ in range(3):
                pacer.wait()
        self.assertEqual(len(sleeps), 2)
        self.assertAlmostEqual(sleeps[0], u.DELAY)
        self.assertAlmostEqual(sleeps[1], u.DELAY * 2)


class Validate(unittest.TestCase):
    """validate_data.py のビッグエコーの元データの検査"""

    OLD = {"sections": [{"header": ["", "月～金"], "rows": [["会員", "300円"]]}] * 6}

    def check(self, item, old=OLD):
        import validate_data as v
        issues = []
        v._bigecho_raw(item, old, "be-1", issues)
        return issues

    def test_shop_that_took_its_tables_down_is_not_an_anomaly(self):
        # 年末の特別料金で表を外した店。変換は金額を出さないので、修復で外して毎回取り直す店にしない
        self.assertEqual(self.check({"sections": [], "price_notes": ["※12/1～1/5は特別料金となります。"], "notice": ""}), [])
        self.assertEqual(self.check({"sections": [], "price_notes": [], "notice": "11/27～1/3は年末特別料金"}), [])
        self.assertEqual(self.check({"sections": self.OLD["sections"][:2], "price_notes": ["◆夜30分料金"], "notice": ""}), [])

    def test_lost_tables_without_a_reason_on_the_page_are_still_an_anomaly(self):
        self.assertTrue(any("大量欠落" in issue for issue in self.check({"sections": self.OLD["sections"][:2], "price_notes": [], "notice": ""})))
        # 見出しを保存していない古い形の記録
        self.assertTrue(any("大量欠落" in issue for issue in self.check({"sections": []})))

    def test_malformed_notice_fields(self):
        self.assertTrue(self.check({"sections": [], "price_notes": "※特別料金"}))
        self.assertTrue(self.check({"sections": [], "price_notes": [], "notice": 1}))
        self.assertTrue(self.check({"sections": [], "price_notes": [], "short": "https://example.test/?p=1"}))
        self.assertEqual(self.check({"sections": [], "price_notes": [], "short": "https://big-echo.jp/?p=22446"}, None), [])


if __name__ == "__main__":
    unittest.main()
