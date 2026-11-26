"""コート・ダジュールの取得（通信の失敗と料金なしの区別、ETag、巡回の打ち切り）を確かめる。通信はしない。"""
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
import update_cotedazur_prices as cote  # noqa: E402
import validate_data  # noqa: E402

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "cotedazur_prices.json").read_text(encoding="utf-8"))
OFFICIAL = {key: FIXTURE["shops"]["cd-10101"][key] for key in ("shop_code", "shop_name", "price_plan")}
IDS = ["cd-10101", "cd-10102", "cd-10103"]


def official(shop_id: str) -> bytes:
    return json.dumps({**OFFICIAL, "shop_code": int(shop_id.removeprefix("cd-"))}, ensure_ascii=False).encode()


def response(status: int = 200, body: bytes = b"", headers: dict | None = None, url: str = "") -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result._content = body
    result.headers.update(headers or {})
    result.url = url
    return result


class FakeFetcher:
    """店ごとに決めた応答を返す。例外を入れておけばそれを投げる。"""

    def __init__(self, answers: dict):
        self.answers = answers
        self.calls = []
        self.bytes = 0

    def get(self, url, *, headers=None, **kwargs):
        shop_id = "cd-" + url.rsplit("/", 1)[1].removesuffix("_price.json")
        self.calls.append((shop_id, dict(headers or {})))
        answer = self.answers[shop_id]
        if isinstance(answer, BaseException):
            raise answer
        answer.url = answer.url or url
        return answer


class UpdateTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        shops = [{"id": shop_id, "chain": "コート・ダジュール", "name": f"店{shop_id}",
                  "url": f"https://www.cotedazur.jp/branch/detail/{shop_id[3:]}.html"} for shop_id in IDS]
        (root / "chains.json").write_text(json.dumps({"shops": shops}, ensure_ascii=False), encoding="utf-8")
        self.data = root / "cotedazur_prices.json"
        for patcher in (mock.patch.object(cote, "DATA_FILE", self.data),
                        mock.patch.object(cote, "SHOPS_FILE", root / "chains.json"),
                        mock.patch.object(cote.time, "sleep"),
                        mock.patch.object(net.time, "sleep"),
                        mock.patch.dict(os.environ, {"CRAWL_DEADLINE": ""})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.due = mock.patch.object(cote.crawl_plan, "due", side_effect=lambda ids, *args, **kwargs: list(ids))
        self.due.start()
        self.addCleanup(self.due.stop)

    def previous(self, shops: dict, updated_at: int = 5) -> None:
        self.data.write_text(json.dumps({"schema_version": 1, "updated_at": updated_at, "shops": shops},
                                        ensure_ascii=False), encoding="utf-8")

    def old(self, shop_id: str) -> dict:
        return {**json.loads(official(shop_id)), "page": "https://www.cotedazur.jp/", "etag": '"old"',
                "last_modified": "Mon, 23 Nov 2026 02:59:59 GMT", "checked_at": 7}

    def run_with(self, answers: dict) -> tuple[bool, dict, FakeFetcher]:
        fetcher = FakeFetcher(answers)
        with mock.patch.object(cote.net, "Fetcher", return_value=fetcher):
            ok = cote.update(log=lambda line: None)
        return ok, json.loads(self.data.read_text(encoding="utf-8")), fetcher

    def test_404_drops_the_previous_price_the_same_day(self):
        self.previous({shop_id: self.old(shop_id) for shop_id in IDS})
        ok, saved, _ = self.run_with({
            "cd-10101": response(404, b"<html>not found</html>"),
            "cd-10102": response(200, official("cd-10102"), {"ETag": '"new"'}),
            "cd-10103": response(304),
        })
        self.assertTrue(ok)
        gone = saved["shops"]["cd-10101"]
        self.assertEqual(gone["price_plan"], [])
        self.assertEqual(gone["no_price"], "公式の料金JSONなし")
        self.assertNotIn("etag", gone)
        self.assertGreater(gone["checked_at"], 7)
        self.assertEqual(saved["shops"]["cd-10102"]["etag"], '"new"')
        self.assertEqual(saved["shops"]["cd-10103"]["etag"], '"old"')
        self.assertGreater(saved["shops"]["cd-10103"]["checked_at"], 7)
        self.assertGreater(saved["updated_at"], 5)

    def test_conditional_request_only_when_the_previous_record_has_prices(self):
        no_price = {**cote.no_price("cd-10102", {"url": "https://www.cotedazur.jp/"}, "x"), "etag": '"stale"'}
        self.previous({"cd-10101": self.old("cd-10101"), "cd-10102": no_price})
        _, _, fetcher = self.run_with({
            "cd-10101": response(304),
            "cd-10102": response(200, official("cd-10102")),
            "cd-10103": response(200, official("cd-10103")),
        })
        calls = dict(fetcher.calls)
        self.assertEqual(calls["cd-10101"], {"If-None-Match": '"old"', "If-Modified-Since": "Mon, 23 Nov 2026 02:59:59 GMT"})
        self.assertEqual(calls["cd-10102"], {})
        self.assertEqual(calls["cd-10103"], {})

    def test_communication_failure_keeps_the_previous_record(self):
        self.previous({shop_id: self.old(shop_id) for shop_id in IDS})
        ok, saved, _ = self.run_with({
            "cd-10101": requests.ReadTimeout("slow"),
            "cd-10102": net.Incomplete("half"),
            "cd-10103": response(502, b"bad gateway"),
        })
        self.assertFalse(ok)
        for shop_id in IDS:
            self.assertEqual(saved["shops"][shop_id], self.old(shop_id))
        self.assertEqual(saved["updated_at"], 5)

    def test_unreadable_json_is_saved_without_prices(self):
        self.previous({shop_id: self.old(shop_id) for shop_id in IDS})
        broken = json.loads(official("cd-10102"))
        for plan in broken["price_plan"]:
            for group in cote.PRICE_GROUPS + tuple(f"solo_{g}" for g in cote.PRICE_GROUPS):
                for row in plan.get(group) or []:
                    for key in list(row):
                        if key.endswith("_30"):
                            row[key] = ""
        _, saved, _ = self.run_with({
            "cd-10101": response(200, b"<html>maintenance</html>"),
            "cd-10102": response(200, json.dumps(broken, ensure_ascii=False).encode()),
            "cd-10103": response(200, official("cd-10103"), url="https://example.com/x.json"),
        })
        for shop_id in IDS:
            self.assertEqual(saved["shops"][shop_id]["price_plan"], [], shop_id)
            self.assertIn("no_price", saved["shops"][shop_id])

    def test_cut_json_is_a_communication_failure(self):
        self.previous({shop_id: self.old(shop_id) for shop_id in IDS})
        body = official("cd-10101")
        ok, saved, _ = self.run_with({
            "cd-10101": response(200, body[: len(body) // 2]),
            "cd-10102": response(304),
            "cd-10103": response(304),
        })
        self.assertFalse(ok)
        self.assertEqual(saved["shops"]["cd-10101"], self.old("cd-10101"))
        self.assertEqual(saved["updated_at"], 5)

    def test_throttled_stops_the_rest_and_keeps_them(self):
        self.previous({shop_id: self.old(shop_id) for shop_id in IDS})
        ok, saved, fetcher = self.run_with({
            "cd-10101": response(200, official("cd-10101"), {"ETag": '"new"'}),
            "cd-10102": response(429),
            "cd-10103": response(200, official("cd-10103")),
        })
        self.assertFalse(ok)
        self.assertEqual([call[0] for call in fetcher.calls], ["cd-10101", "cd-10102"])
        self.assertEqual(saved["shops"]["cd-10101"]["etag"], '"new"')
        self.assertEqual(saved["shops"]["cd-10102"], self.old("cd-10102"))

    def test_out_of_time_saves_what_was_fetched_and_ends_normally(self):
        self.previous({shop_id: self.old(shop_id) for shop_id in IDS})
        ok, saved, fetcher = self.run_with({
            "cd-10101": response(200, official("cd-10101"), {"ETag": '"new"'}),
            "cd-10102": net.OutOfTime("late"),
            "cd-10103": response(200, official("cd-10103")),
        })
        self.assertTrue(ok)
        self.assertEqual(len(fetcher.calls), 2)
        self.assertEqual(saved["shops"]["cd-10101"]["etag"], '"new"')
        self.assertEqual(saved["shops"]["cd-10102"], self.old("cd-10102"))
        self.assertEqual(saved["updated_at"], 5)

    def test_only_shops_due_today_are_fetched(self):
        self.due.stop()
        with mock.patch.object(cote.crawl_plan, "due", return_value=["cd-10102"]) as due:
            self.previous({shop_id: self.old(shop_id) for shop_id in IDS})
            _, saved, fetcher = self.run_with({"cd-10102": response(304)})
        self.assertEqual(due.call_args.args[2], "source/cotedazur_prices.json")
        self.assertEqual([call[0] for call in fetcher.calls], ["cd-10102"])
        self.assertEqual(saved["shops"]["cd-10101"], self.old("cd-10101"))
        self.due.start()

    def test_stops_after_failures_in_a_row(self):
        ids = [f"cd-{10101 + i}" for i in range(8)]
        shops = [{"id": shop_id, "chain": "コート・ダジュール", "name": shop_id, "url": "https://www.cotedazur.jp/"}
                 for shop_id in ids]
        cote.SHOPS_FILE.write_text(json.dumps({"shops": shops}), encoding="utf-8")
        self.previous({})
        ok, saved, fetcher = self.run_with({shop_id: requests.ConnectTimeout("x") for shop_id in ids})
        self.assertFalse(ok)
        self.assertEqual(len(fetcher.calls), cote.MAX_STREAK)
        self.assertEqual(saved["shops"], {})

    def test_reads_again_on_timeout_through_the_shared_fetcher(self):
        self.previous({})
        answers = iter([requests.ReadTimeout("slow"), response(200, official("cd-10101"), {"ETag": '"e"'},
                                                               url="https://www.cotedazur.jp/public/10101_price.json")])

        def get(self, url, **kwargs):
            answer = next(answers)
            if isinstance(answer, BaseException):
                raise answer
            return answer

        fetcher = net.Fetcher("test")
        with mock.patch.object(requests.Session, "get", get):
            record = cote.fetch_one(fetcher, "cd-10101", {"url": "https://www.cotedazur.jp/"}, None)
        self.assertEqual(record["etag"], '"e"')
        self.assertTrue(record["price_plan"])


class ValidateTest(unittest.TestCase):
    def test_no_price_record_is_accepted_and_skips_the_drop_check(self):
        issues = []
        record = cote.no_price("cd-10457", {"name": "店", "url": "https://www.cotedazur.jp/"}, "公式の料金JSONなし")
        old = {**OFFICIAL, "shop_code": 10457}
        validate_data._cotedazur_prices(record, old, "cd-10457", issues)
        self.assertEqual(issues, [])
        validate_data._cotedazur_prices({**record, "price_plan": OFFICIAL["price_plan"]}, None, "cd-10457", issues)
        self.assertEqual(len(issues), 1)

    def test_built_record_switching_time_must_match(self):
        built = {"checked_at": 1, "regular": [], "solo": []}
        for kind in ("regular", "solo"):
            built[kind] = [{"band": "通常料金", "customers": ["会員"], "plans": [
                {"name": "30分", "hours": "開店～17:00", "days": [{"day": "月火水木", "prices": {"会員": 300}}]},
                {"name": "30分", "hours": "17:00～閉店", "days": [{"day": "月火水木", "prices": {"会員": 460}}]},
            ]}]
        raw = {"cd-10150": {"price_plan": [{"switching_time": "17:00:00"}]}}
        with mock.patch.object(validate_data, "_cotedazur_raw", return_value=raw):
            issues = []
            validate_data._cotedazur_prices(built, None, "cd-10150", issues)
            self.assertEqual(issues, [])
            raw["cd-10150"]["price_plan"][0]["switching_time"] = "18:00:00"
            validate_data._cotedazur_prices(built, None, "cd-10150", issues)
            self.assertEqual(issues, ["cd-10150: 昼夜の切り替え時刻が元データと一致しません"])
            issues = []
            built["solo"][0]["plans"][0]["hours"] = "開店～18:00"
            validate_data._cotedazur_prices(built, None, "cd-10150", issues)
            self.assertEqual(len(issues), 1)

    def test_built_record_from_a_multi_period_source_is_rejected(self):
        built = {"checked_at": 1}
        for kind in ("regular", "solo"):
            built[kind] = [{"band": "通常料金", "customers": ["会員"], "plans": [
                {"name": "30分", "hours": "開店～18:00", "days": [{"day": "月火水木", "prices": {"会員": 300}}]},
                {"name": "30分", "hours": "18:00～閉店", "days": [{"day": "月火水木", "prices": {"会員": 460}}]},
            ]}]
        plan = {"switching_time": "18:00:00"}
        for raw, expected in (({"price_plan": [plan, plan]}, 1), ({"price_plan": [], "no_price": "x"}, 1),
                              ({"price_plan": [plan]}, 0)):
            with mock.patch.object(validate_data, "_cotedazur_raw", return_value={"cd-10101": raw}):
                issues = []
                validate_data._cotedazur_prices(built, None, "cd-10101", issues)
                self.assertEqual(len(issues), expected, (raw, issues))


if __name__ == "__main__":
    unittest.main()
