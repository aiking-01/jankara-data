"""実行: python -m unittest discover tests"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import update_chain_prices as u  # noqa: E402

PAGE = """
<html><body>
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


class ParsePage(unittest.TestCase):
    def test_reads_30min_tables_and_hours(self):
        parsed = u.parse_page(PAGE)
        self.assertEqual(parsed["hours"], "月曜日 9:00～2:00日曜・祝祭日 9:00～2:00")
        self.assertEqual(len(parsed["sections"]), 1)
        section = parsed["sections"][0]
        self.assertEqual(section["time"], "OPEN～18：00")
        self.assertEqual(section["title"], "1ドリンクオーダー制")
        self.assertEqual(section["note"], "※注意書き")
        self.assertEqual(section["header"], ["", "月～金", "土・日・祝日"])
        self.assertEqual(section["rows"], [["ひとりカラオケ(会員の場合)", "200円", "300円"]])

    def test_page_without_prices(self):
        self.assertIsNone(u.parse_page("<html><body>メンテナンス中</body></html>"))


def response(text="", payload=None, status=200, url="https://big-echo.jp/?p=1"):
    result = mock.Mock(status_code=status, text=text, url=url)
    result.json.return_value = payload
    result.raise_for_status.side_effect = None if status == 200 else u.requests.HTTPError(str(status))
    return result


class Update(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for target, attribute, value in [
            (u, "DATA_FILE", Path(folder.name) / "chain_prices.json"),
            (u, "DELAY", 0),
        ]:
            patcher = mock.patch.object(target, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def stored(self, shops, updated_at=1):
        u.save(shops, updated_at)

    def read(self):
        return json.loads(u.DATA_FILE.read_text(encoding="utf-8"))

    def run_update(self, ids, get):
        session = mock.Mock(headers={})
        session.get.side_effect = get
        with mock.patch.object(u, "shop_ids", return_value=ids), mock.patch.object(u.requests, "Session", return_value=session):
            ok = u.update(log=lambda line: None)
        return ok, session

    def test_fetches_and_remembers_page_url(self):
        def get(url, timeout):
            if "api/point" in url:
                return response(payload={"短縮ＵＲＬ": "https://big-echo.jp/?p=1"})
            return response(PAGE)
        ok, session = self.run_update(["be-0001"], get)
        self.assertTrue(ok)
        shop = self.read()["shops"]["be-0001"]
        self.assertEqual(shop["page"], "https://big-echo.jp/?p=1")
        self.assertEqual(len(shop["sections"]), 1)
        self.assertGreater(self.read()["updated_at"], 1)
        # 次回は保存した URL を使い、API を呼ばない
        shops = self.read()["shops"]
        shops["be-0001"]["checked_at"] = 0
        self.stored(shops)
        _, session = self.run_update(["be-0001"], lambda url, timeout: response(PAGE))
        self.assertEqual([call.args[0] for call in session.get.call_args_list], ["https://big-echo.jp/?p=1"])

    def test_remembers_redirect_target_on_https(self):
        def get(url, timeout):
            if "api/point" in url:
                return response(payload={"短縮ＵＲＬ": "https://big-echo.jp/?p=1"})
            return response(PAGE, url="http://big-echo.jp/shop_info/a")
        self.run_update(["be-0001"], get)
        self.assertEqual(self.read()["shops"]["be-0001"]["page"], "https://big-echo.jp/shop_info/a")

    def test_redirect_outside_official_site_is_not_remembered(self):
        known = "https://big-echo.jp/?p=1"
        self.stored({"be-0001": {"hours": "", "sections": [{}], "page": known, "checked_at": 0}})
        self.run_update(["be-0001"], lambda url, timeout: response(PAGE, url="https://example.test/"))
        self.assertEqual(self.read()["shops"]["be-0001"]["page"], known)

    def test_temporary_error_is_retried_once(self):
        self.stored({"be-0001": {"hours": "", "sections": [{}], "page": "https://big-echo.jp/?p=1", "checked_at": 0}})
        replies = [response(status=502), response(PAGE)]
        ok, session = self.run_update(["be-0001"], lambda url, timeout: replies.pop(0))
        self.assertTrue(ok)
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(len(self.read()["shops"]["be-0001"]["sections"]), 1)

    def test_stops_fetching_when_throttled(self):
        shops = {f"be-{i:04}": {"hours": "h", "sections": [{"time": "old"}], "page": "https://big-echo.jp/?p=1", "checked_at": 0} for i in range(50)}
        self.stored(shops, updated_at=5)
        ok, session = self.run_update(list(shops), lambda url, timeout: response(status=429))
        self.assertFalse(ok)
        self.assertLess(session.get.call_count, 50)
        self.assertEqual(self.read()["shops"]["be-0030"]["sections"], [{"time": "old"}])
        self.assertEqual(self.read()["updated_at"], 5)

    def test_recent_shops_are_skipped(self):
        self.stored({"be-0001": {"hours": "", "sections": [{}], "page": "https://big-echo.jp/?p=1", "checked_at": int(time.time())}})
        _, session = self.run_update(["be-0001"], lambda url, timeout: response(PAGE))
        session.get.assert_not_called()

    def test_failure_keeps_previous_and_does_not_advance_timestamp(self):
        old = {"hours": "h", "sections": [{"time": "old"}], "page": "https://big-echo.jp/?p=1", "checked_at": 0}
        self.stored({"be-0001": old}, updated_at=5)
        ok, _ = self.run_update(["be-0001"], lambda url, timeout: response(status=500))
        self.assertFalse(ok)
        self.assertEqual(self.read()["shops"]["be-0001"]["sections"], [{"time": "old"}])
        self.assertEqual(self.read()["updated_at"], 5)

    def test_site_change_keeps_previous_values(self):
        shops = {f"be-{i:04}": {"hours": "h", "sections": [{"time": "old"}], "page": "https://big-echo.jp/?p=1", "checked_at": 0} for i in range(10)}
        self.stored(shops, updated_at=5)
        empty = "<html><body><div id='price'></div></body></html>"
        ok, _ = self.run_update(list(shops), lambda url, timeout: response(empty))
        self.assertFalse(ok)
        self.assertEqual(self.read()["shops"]["be-0003"]["sections"], [{"time": "old"}])

    def test_closed_shop_is_removed(self):
        recent = int(time.time())
        ids = [f"be-{i:04}" for i in range(10)]
        self.stored({shop_id: {"hours": "", "sections": [{}], "checked_at": recent} for shop_id in ids})
        self.run_update(ids[:9], lambda url, timeout: response(PAGE))
        self.assertEqual(list(self.read()["shops"]), ids[:9])

    def test_unexpected_page_host_is_rejected(self):
        ok, _ = self.run_update(["be-0001"], lambda url, timeout: response(payload={"短縮ＵＲＬ": "https://example.test/"}))
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
