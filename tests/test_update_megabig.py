"""メガビッグの取得（update_megabig.py）と元データの検査を、見本の HTML（tests/fixtures/megabig）で確かめる。通信はしない。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import net  # noqa: E402
import update_megabig as mb  # noqa: E402
import validate_data as v  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "megabig"
NOW = 1795000000  # 2026-11-17


def page(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def parse(shop_id: str, markup: str | None = None):
    return mb.parse_shop(shop_id, f"https://megakara.jp/shop/{shop_id}/", markup or page(f"shop_{shop_id}.html"), NOW)


class FakeResponse:
    def __init__(self, text: str = "", status: int = 200, url: str | None = None):
        self.text = text
        self.status_code = status
        self.encoding = None
        self.url = url


class FakeFetcher:
    """URL ごとに応答か例外を返す。読んだ URL を順に残す。"""

    def __init__(self, answers: dict):
        self.answers = answers
        self.calls = []

    def get(self, url, html=False):
        self.calls.append(url)
        answer = self.answers[url]
        if isinstance(answer, BaseException):
            raise answer
        return answer


class ParseTests(unittest.TestCase):
    def test_reads_a_shop_page(self):
        shop, record = parse("1389")
        self.assertEqual(shop["name"], "布施駅前店")
        self.assertEqual(shop["prefecture"], "大阪府")
        self.assertEqual(len(record["sections"]), 8)
        self.assertNotIn("missing", record)
        self.assertEqual(record["checked_at"], NOW)
        # 金額は数字だけ（カンマなし）
        self.assertEqual(record["sections"][2]["rows"][1], ["学生", "-", "1260"])

    def test_dash_cells_are_kept_as_no_price(self):
        _, record = parse("1389")
        pack = next(section for section in record["sections"] if section["time"].startswith("3時間パック"))
        self.assertEqual([row[1] for row in pack["rows"]], ["-"] * 5)
        self.assertEqual(pack["rows"][3], ["一般", "-", "1580"])

    def test_html_comments_are_removed_before_reading(self):
        # 去年の告知「表示料金は11月27日(木)まで」と夏季料金の PDF のリンクはコメントの中にある
        _, record = parse("1434")
        texts = " ".join(record["notices"] + [section["heading"] for section in record["sections"]])
        self.assertNotIn("表示料金", texts)
        self.assertNotIn("夏季", texts)
        self.assertEqual(record["links"], [])
        self.assertEqual(record["sections"][0]["header"], ["30分", "月〜金・祝前", "土・日・祝"])

    def test_uncommented_notice_and_pdf_link_are_kept_for_the_conversion(self):
        markup = page("shop_1434.html").replace("<!--<span", "<span").replace("</span>--></h3>", "</span></h3>")
        _, record = parse("1434", markup)
        self.assertTrue(any("夏季料金" in text for text in record["notices"]))
        self.assertEqual(record["links"], ["https://megakara.jp/wp-content/uploads/2026/08/0827_1434.pdf"])

    def test_cell_with_a_note_is_not_read_as_a_number(self):
        markup = page("shop_0907.html").replace('<span class="num">1,400</span>', '<span class="num">980円※2</span>', 1)
        _, record = parse("0907", markup)
        self.assertEqual(len(record["sections"]), 7)
        self.assertEqual(record["skipped"], [{
            "time": "昼3時間パック OPEN〜19:00", "title": "3時間パック",
            "note": "アルコール・ソフトドリンク飲み放題込み LINEで簡単会員登録（無料） 最終受付（16:00）",
            "reason": "price-cell"}])
        for section in record["sections"]:
            for row in section["rows"]:
                self.assertTrue(all(value.isdigit() or value == "-" for value in row[1:]), row)

    def test_skipped_table_keeps_its_note(self):
        # 捨てた表の備考に書かれた期間料金の告知も、変換から見えるように残す
        markup = page("shop_0907.html").replace('<span class="num">1,400</span>', '<span class="num">1,400※</span>', 1)
        markup = markup.replace("最終受付（16:00）</p>", "最終受付（16:00）</p><p>※12月29日(火)～1月3日(日)は年末年始料金となります</p>", 1)
        _, record = parse("0907", markup)
        self.assertEqual(len(record["skipped"]), 1)
        self.assertIn("12月29日(火)～1月3日(日)は年末年始料金", record["skipped"][0]["note"])

    def test_amount_with_yen_and_comma_is_read(self):
        markup = page("shop_0907.html").replace('<span class="num">1,400</span>', '<span class="num">￥1,400円</span>', 1)
        _, record = parse("0907", markup)
        self.assertNotIn("skipped", record)
        self.assertEqual(record["sections"][2]["rows"][0], ["学生会員", "800", "1100"])

    def test_page_without_price_table_has_no_prices(self):
        markup = page("shop_0907.html").replace('id="price"', 'id="other"')
        shop, record = parse("0907", markup)
        self.assertEqual(shop["name"], "高槻2号店")
        self.assertEqual(record["missing"], "no-price-table")
        self.assertEqual(record["sections"], [])

    def test_page_without_slot_table_has_no_prices(self):
        markup = page("shop_0907.html").replace("<i>30</i>分", "<i>60</i>分")
        _, record = parse("0907", markup)
        self.assertEqual(record["missing"], "no-slot-table")

    def test_unreadable_shop_page_has_no_prices(self):
        shop, record = parse("0907", "<html><body><p>ただいまメンテナンス中です</p></body></html>")
        self.assertIsNone(shop)
        self.assertEqual(record["missing"], "unreadable-shop")

    def test_table_outside_the_price_boxes_is_reported(self):
        markup = page("shop_0907.html").replace('<h2>料金案内</h2>', '<h2>料金案内</h2><table><tr><td>年末年始 30分 500</td></tr></table>')
        _, record = parse("0907", markup)
        self.assertEqual(record["unread_tables"], 1)

    def test_index_lists_shops(self):
        urls = mb.shop_urls(page("index.html"))
        self.assertEqual(len(urls), 11)
        self.assertEqual(urls["mb-1389"], "https://megakara.jp/shop/1389/")


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.chain_file = root / "chains.json"
        self.price_file = root / "megabig_prices.json"
        # リポジトリの source/seen.json は読まない（公開のたびに変わる）
        empty = {"schema_version": 1, "files": {}, "tried": {}, "lists": {}}
        for patcher in (mock.patch.object(mb, "CHAIN_FILE", self.chain_file),
                        mock.patch.object(mb, "PRICE_FILE", self.price_file),
                        mock.patch.object(mb.crawl_plan.seen, "load", side_effect=lambda *args: json.loads(json.dumps(empty))),
                        mock.patch.object(mb.time, "sleep"),
                        mock.patch.dict(os.environ, {"CRAWL_ALL": "1", "JANKARA_TESTING": "1", "GITHUB_ACTIONS": ""})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.log = []

    def write(self, rows: dict, prices: dict):
        other = {"id": "be-1", "chain": "ビッグエコー", "name": "どこか店"}
        self.chain_file.write_text(json.dumps({"schema_version": 1, "updated_at": 1, "shops": [other, *rows.values()]},
                                              ensure_ascii=False), encoding="utf-8")
        self.price_file.write_text(json.dumps({"schema_version": 1, "updated_at": 1, "shops": prices},
                                              ensure_ascii=False), encoding="utf-8")

    def read(self):
        chains = json.loads(self.chain_file.read_text(encoding="utf-8"))
        prices = json.loads(self.price_file.read_text(encoding="utf-8"))
        return {row["id"]: row for row in chains["shops"]}, prices["shops"]

    @staticmethod
    def row(key: str, name: str = "前の店") -> dict:
        return {"id": key, "chain": "メガビッグ", "name": name, "url": f"https://megakara.jp/shop/{key[3:]}/"}

    @staticmethod
    def old_price(key: str) -> dict:
        return {"hours": "9:00～5:00", "sections": [{"heading": "", "time": "OPEN〜18:00", "title": "30分", "note": "",
                                                     "header": ["30分", "月〜金", "土・日・祝"], "rows": [["一般", "300", "400"]]}],
                "notices": [], "links": [], "page": f"https://megakara.jp/shop/{key[3:]}/", "scope": "all", "checked_at": 1}

    def index(self, *ids: str) -> FakeResponse:
        links = "".join(f'<a href="/shop/{shop_id}/">{shop_id}</a>' for shop_id in ids)
        return FakeResponse(f"<html><body>{links}</body></html>")

    def run_update(self, answers: dict, now: int = NOW):
        fetcher = FakeFetcher(answers)
        result = mb.update(log=self.log.append, fetcher=fetcher, now=now)
        return result, fetcher

    def test_network_failure_keeps_the_last_record_and_missing_table_does_not(self):
        keys = ["mb-0907", "mb-1389", "mb-1434", "mb-1383"]
        self.write({key: self.row(key) for key in keys}, {key: self.old_price(key) for key in keys})
        no_table = page("shop_0907.html").replace('id="price"', 'id="other"')
        result, fetcher = self.run_update({
            mb.INDEX_URL: self.index("0907", "1389", "1434", "1383"),
            "https://megakara.jp/shop/0907/": FakeResponse(no_table),
            "https://megakara.jp/shop/1383/": requests.ReadTimeout("slow"),
            "https://megakara.jp/shop/1389/": FakeResponse(page("shop_1389.html")),
            "https://megakara.jp/shop/1434/": FakeResponse("not found", 404),
        })
        self.assertFalse(result)  # 通信の失敗が 1 店ある
        rows, prices = self.read()
        self.assertEqual(prices["mb-1383"], self.old_price("mb-1383"))  # 前回の記録のまま（確認日時も進まない）
        self.assertEqual(prices["mb-0907"]["missing"], "no-price-table")  # 前回の金額を残さない
        self.assertEqual(prices["mb-0907"]["sections"], [])
        self.assertEqual(rows["mb-0907"]["name"], "高槻2号店")
        self.assertEqual(prices["mb-1434"]["missing"], "not-found")
        self.assertEqual(len(prices["mb-1389"]["sections"]), 8)
        self.assertEqual(rows["mb-1389"]["name"], "布施駅前店")
        self.assertEqual(rows["mb-1383"]["name"], "前の店")
        self.assertIn("be-1", rows)  # ほかのチェーンの行は残す
        self.assertEqual(fetcher.calls[0], mb.INDEX_URL)

    def test_shop_gone_from_the_list_loses_its_prices_but_keeps_its_row(self):
        keys = ["mb-0907", "mb-1389"]
        self.write({key: self.row(key) for key in keys}, {key: self.old_price(key) for key in keys})
        result, fetcher = self.run_update({
            mb.INDEX_URL: self.index("1389"),
            "https://megakara.jp/shop/1389/": FakeResponse(page("shop_1389.html")),
        })
        self.assertTrue(result)
        rows, prices = self.read()
        self.assertNotIn("mb-0907", prices)
        self.assertIn("mb-0907", rows)  # 店の一覧の行は検査で止められないよう残す
        self.assertNotIn("https://megakara.jp/shop/0907/", fetcher.calls)
        # 次の回も取りに行かず、後から消えた店の数にも数えない
        rows["mb-0937"] = self.row("mb-0937")
        rows["mb-1101"] = self.row("mb-1101")
        prices.update({key: self.old_price(key) for key in ("mb-0937", "mb-1101")})
        self.write({key: row for key, row in rows.items() if key.startswith("mb-")}, prices)
        _, fetcher = self.run_update({
            mb.INDEX_URL: self.index("1389"),
            "https://megakara.jp/shop/1389/": FakeResponse(page("shop_1389.html")),
        })
        _, prices = self.read()
        self.assertEqual(sorted(prices), ["mb-1389"])
        self.assertEqual(fetcher.calls, [mb.INDEX_URL])  # 1389 は確かめたばかり

    def test_many_shops_gone_at_once_are_not_removed(self):
        keys = ["mb-0907", "mb-0937", "mb-1101", "mb-1389"]
        self.write({key: self.row(key) for key in keys}, {key: self.old_price(key) for key in keys})
        answers = {mb.INDEX_URL: self.index("1389"),
                   "https://megakara.jp/shop/1389/": FakeResponse(page("shop_1389.html"))}
        for key in ("mb-0907", "mb-0937", "mb-1101"):
            answers[f"https://megakara.jp/shop/{key[3:]}/"] = requests.ConnectionError("down")
        self.run_update(answers)
        _, prices = self.read()
        self.assertEqual(sorted(prices), keys)

    def test_closed_shop_is_not_read_and_loses_its_prices(self):
        keys = ["mb-1406", "mb-1389"]
        rows = {key: self.row(key) for key in keys}
        rows["mb-1406"]["name"] = "天満本店【2026/9/29閉店】"
        self.write(rows, {key: self.old_price(key) for key in keys})
        result, fetcher = self.run_update({
            mb.INDEX_URL: self.index("1389", "1406"),
            "https://megakara.jp/shop/1389/": FakeResponse(page("shop_1389.html")),
        })
        self.assertTrue(result)
        rows, prices = self.read()
        self.assertNotIn("mb-1406", prices)
        self.assertIn("mb-1406", rows)
        self.assertNotIn("https://megakara.jp/shop/1406/", fetcher.calls)

    def test_deadline_stops_and_saves_what_was_read(self):
        keys = ["mb-0907", "mb-1389"]
        self.write({key: self.row(key) for key in keys}, {key: self.old_price(key) for key in keys})
        result, _ = self.run_update({
            mb.INDEX_URL: self.index("0907", "1389"),
            "https://megakara.jp/shop/0907/": FakeResponse(page("shop_0907.html")),
            "https://megakara.jp/shop/1389/": net.OutOfTime("late"),
        })
        self.assertTrue(result)  # 締め切りは失敗ではない
        _, prices = self.read()
        self.assertEqual(len(prices["mb-0907"]["sections"]), 8)
        self.assertEqual(prices["mb-1389"], self.old_price("mb-1389"))

    def test_rate_limit_stops_and_keeps_the_rest(self):
        keys = ["mb-0907", "mb-1389"]
        self.write({key: self.row(key) for key in keys}, {key: self.old_price(key) for key in keys})
        result, fetcher = self.run_update({
            mb.INDEX_URL: self.index("0907", "1389"),
            "https://megakara.jp/shop/0907/": FakeResponse("busy", 429),
            "https://megakara.jp/shop/1389/": FakeResponse(page("shop_1389.html")),
        })
        self.assertFalse(result)
        _, prices = self.read()
        self.assertEqual(prices["mb-1389"], self.old_price("mb-1389"))
        self.assertNotIn("https://megakara.jp/shop/1389/", fetcher.calls)

    def test_redirected_shop_page_has_no_prices(self):
        self.write({"mb-1406": self.row("mb-1406")}, {"mb-1406": self.old_price("mb-1406")})
        self.run_update({
            mb.INDEX_URL: self.index("1406"),
            "https://megakara.jp/shop/1406/": FakeResponse(page("shop_0907.html"), url=mb.INDEX_URL),
        })
        rows, prices = self.read()
        self.assertEqual(prices["mb-1406"]["missing"], "not-found")
        self.assertEqual(rows["mb-1406"]["name"], "前の店")  # 転送先の店の情報で書き換えない

    def test_index_failure_changes_nothing(self):
        self.write({"mb-0907": self.row("mb-0907")}, {"mb-0907": self.old_price("mb-0907")})
        before = self.price_file.read_text(encoding="utf-8")
        result, _ = self.run_update({mb.INDEX_URL: requests.ConnectTimeout("no")})
        self.assertFalse(result)
        self.assertEqual(self.price_file.read_text(encoding="utf-8"), before)

    def test_new_shop_with_unreadable_page_is_not_added(self):
        self.write({}, {})
        self.run_update({
            mb.INDEX_URL: self.index("2000"),
            "https://megakara.jp/shop/2000/": FakeResponse("<html><body>工事中</body></html>"),
        })
        rows, prices = self.read()
        self.assertNotIn("mb-2000", rows)
        self.assertNotIn("mb-2000", prices)

    def test_only_due_shops_are_read(self):
        keys = [f"mb-{number}" for number in ("0907", "0937", "1101", "1379", "1380", "1383")]
        prices = {key: {**self.old_price(key), "checked_at": NOW - 86400 * (index + 1)} for index, key in enumerate(keys)}
        self.write({key: self.row(key) for key in keys}, prices)
        answers = {mb.INDEX_URL: self.index(*(key[3:] for key in keys))}
        for key in keys:
            answers[f"https://megakara.jp/shop/{key[3:]}/"] = FakeResponse(page("shop_0907.html"))
        with mock.patch.dict(os.environ, {"CRAWL_ALL": ""}):
            _, fetcher = self.run_update(answers)
        # 3 日に 1 回、古い順に 6 店の 3 分の 1
        self.assertEqual(fetcher.calls[1:], ["https://megakara.jp/shop/1383/", "https://megakara.jp/shop/1380/"])


class ValidateTests(unittest.TestCase):
    def record(self):
        _, record = parse("1389")
        return record

    def test_dash_cells_and_digits_pass(self):
        issues = []
        v._megabig_raw(self.record(), None, "mb-1389", issues)
        self.assertEqual(issues, [])

    def test_missing_record_passes(self):
        issues = []
        record = mb.missing_record("https://megakara.jp/shop/0907/", "", NOW, "no-price-table")
        v._megabig_raw(record, self.record(), "mb-0907", issues)
        self.assertEqual(issues, [])
        v._megabig_raw({**record, "missing": "other"}, None, "mb-0907", issues)
        self.assertEqual(len(issues), 1)

    def test_joined_or_formatted_amounts_fail(self):
        for value in ("9802※", "1,260", "¥980", "", "980円"):
            record = self.record()
            record["sections"][0]["rows"][0][1] = value
            issues = []
            v._megabig_raw(record, None, "mb-1389", issues)
            self.assertTrue(any("金額が不正" in issue for issue in issues), value)

    def test_skipped_tables_are_not_a_mass_loss(self):
        old = self.record()
        new = self.record()
        new["skipped"] = [{"time": section["time"], "title": section["title"], "reason": "price-cell"}
                          for section in new["sections"][2:]]
        new["sections"] = new["sections"][:2]
        issues = []
        v._megabig_raw(new, old, "mb-1389", issues)
        self.assertEqual(issues, [])
        del new["skipped"]
        v._megabig_raw(new, old, "mb-1389", issues)
        self.assertTrue(any("大量欠落" in issue for issue in issues))

    def test_skipped_table_note_must_be_text(self):
        record = self.record()
        record["skipped"] = [{"time": "", "title": "3時間パック", "note": "最終受付（16:00）", "reason": "price-cell"}]
        issues = []
        v._megabig_raw(record, None, "mb-1389", issues)
        self.assertEqual(issues, [])
        record["skipped"][0]["note"] = ["最終受付（16:00）"]
        v._megabig_raw(record, None, "mb-1389", issues)
        self.assertTrue(any("読めなかった表の記録が不正" in issue for issue in issues))


if __name__ == "__main__":
    unittest.main()
