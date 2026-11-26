"""カラオケ館の料金画像を確かめる手順（ocr/plan.py、ocr/merge.py、ocr.yml）を確かめる。通信はしない。"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ocr"))
sys.path.insert(0, str(ROOT / "scripts"))
import merge  # noqa: E402
import net  # noqa: E402
import plan  # noqa: E402

NOW = 1_800_000_000


def jpeg(color: str = "white") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buffer, "JPEG")
    return buffer.getvalue()


def response(body: bytes, status: int = 200) -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result._content = body
    return result


class FakeFetcher:
    def __init__(self, result):
        self.result = result
        self.urls = []
        self.bytes = 0

    def get(self, url, **kwargs):
        self.urls.append(url)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


TARGET = {"id": "kan-61", "chain": "カラオケ館", "url": "https://karaokekan.net/shop2price/image/61/0", "key": None, "reads": 2}


class PlanTests(unittest.TestCase):
    def setUp(self):
        mock.patch.object(plan.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name)

    def check(self, result, stored=None):
        checks, target = {}, dict(TARGET)
        chosen = plan.check_kan(FakeFetcher(result), target, stored or {}, checks, self.work)
        return chosen, checks, target

    def test_same_image_is_recorded_and_not_read_again(self):
        data = jpeg()
        key = hashlib.sha256(data).hexdigest()
        chosen, checks, _ = self.check(response(data), {"kan-61": {"key": key}})
        self.assertFalse(chosen)
        self.assertEqual(checks["kan-61"]["key"], key)
        self.assertEqual(list(self.work.iterdir()), [])

    def test_changed_image_is_recorded_and_read(self):
        chosen, checks, target = self.check(response(jpeg("black")), {"kan-61": {"key": "a" * 64}})
        self.assertTrue(chosen)
        self.assertEqual(checks["kan-61"]["key"], target["key"])
        self.assertTrue((self.work / "kan-61.jpg").exists())

    def test_missing_or_non_image_answer_removes_the_price_on_the_same_day(self):
        _, checks, _ = self.check(response(b"", 404))
        self.assertEqual(checks["kan-61"]["key"], "missing")
        _, checks, _ = self.check(response(b"<html><body>maintenance</body></html>"))
        self.assertEqual(checks["kan-61"]["key"], "not-image")

    def test_network_failures_keep_the_previous_record(self):
        for result in (requests.ReadTimeout("slow"), net.Incomplete("cut"), response(b"", 502), response(jpeg()[:-200])):
            with self.subTest(result=result), self.assertRaises(plan.FetchFailed):
                self.check(result)
        with self.assertRaises(plan.Refused):
            self.check(response(b"", 429))
        with self.assertRaises(net.OutOfTime):
            self.check(net.OutOfTime("late"))

    def test_recently_verified_images_are_not_fetched_again(self):
        record = {"key": "a" * 64, "latest_key": "a" * 64, "verified_at": NOW - 86400}
        self.assertTrue(plan.recently_verified(record, NOW))
        self.assertFalse(plan.recently_verified({**record, "verified_at": NOW - 3 * 86400}, NOW))
        # 画像が変わってまだ読めていない店と、記録のない店は毎回取る
        self.assertFalse(plan.recently_verified({**record, "latest_key": "b" * 64}, NOW))
        self.assertFalse(plan.recently_verified({"key": "a" * 64}, NOW))

    def test_main_stops_at_the_deadline_and_keeps_what_was_checked(self):
        data = jpeg()
        key = hashlib.sha256(data).hexdigest()
        targets = [dict(TARGET, id=f"kan-{n}", url=f"https://karaokekan.net/shop2price/image/{n}/0") for n in (1, 2, 3)]
        results = iter([response(data), net.OutOfTime("late")])

        class Fetcher:
            bytes = 0

            def __init__(self, *args, **kwargs):
                pass

            def get(self, url, **kwargs):
                result = next(results)
                if isinstance(result, Exception):
                    raise result
                return result

        stored = {"kan-1": {"key": key}}
        with mock.patch.object(plan, "maneki_targets", return_value=[]), \
                mock.patch.object(plan, "kan_targets", return_value=targets), \
                mock.patch.object(plan, "load_stored", return_value=stored), \
                mock.patch.object(plan.net, "Fetcher", Fetcher), \
                mock.patch.object(sys, "argv", ["plan.py", str(self.work)]):
            plan.main()
        self.assertEqual(json.loads((self.work / "targets.json").read_text(encoding="utf-8")), [])
        checks = json.loads((self.work / "kan_checks.json").read_text(encoding="utf-8"))
        self.assertEqual(list(checks), ["kan-1"])
        self.assertEqual(checks["kan-1"]["key"], key)


class MergeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.file = self.dir / "ocr.json"
        patcher = mock.patch.object(merge, "OCR_FILE", self.file)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.file.write_text(json.dumps({"schema_version": 1, "updated_at": 1, "shops": {
            "kan-1": {"chain": "カラオケ館", "key": "a" * 64, "reads": ["x", "y"], "checked_at": NOW - 30 * 86400},
            "kan-2": {"chain": "カラオケ館", "key": "b" * 64, "reads": ["x", "y"], "checked_at": NOW - 30 * 86400,
                      "latest_key": "b" * 64, "verified_at": NOW - 86400},
            "mn-1": {"chain": "まねきねこ", "key": "pdf#1", "reads": ["x"], "checked_at": NOW},
        }}), encoding="utf-8")

    def run_merge(self, checks: dict, *results: list) -> dict:
        args = ["merge.py"]
        if checks is not None:
            (self.dir / "checks.json").write_text(json.dumps(checks), encoding="utf-8")
            args += ["--checks", str(self.dir / "checks.json")]
        for number, result in enumerate(results):
            (self.dir / f"reads{number}.json").write_text(json.dumps(result), encoding="utf-8")
            args.append(str(self.dir / f"reads{number}.json"))
        with mock.patch.object(sys, "argv", args):
            merge.main()
        return json.loads(self.file.read_text(encoding="utf-8"))["shops"]

    def test_run_without_changed_images_still_records_the_check(self):
        shops = self.run_merge({"kan-1": {"key": "a" * 64, "at": NOW}, "kan-2": {"key": "c" * 64, "at": NOW}})
        self.assertEqual(shops["kan-1"]["latest_key"], "a" * 64)
        self.assertEqual(shops["kan-1"]["verified_at"], NOW)
        # 画像が変わった店は確かめた時刻を進めない（変換で外れる）
        self.assertEqual(shops["kan-2"]["latest_key"], "c" * 64)
        self.assertEqual(shops["kan-2"]["verified_at"], NOW - 86400)
        self.assertNotIn("latest_key", shops["mn-1"])

    def test_new_reads_are_verified_by_the_check_of_the_same_run(self):
        result = [{"id": "kan-2", "chain": "カラオケ館", "url": "u", "key": "c" * 64, "reads": ["p", "q"], "checked_at": NOW}]
        shops = self.run_merge({"kan-2": {"key": "c" * 64, "at": NOW - 600}}, result)
        self.assertEqual(shops["kan-2"]["reads"], ["p", "q"])
        self.assertEqual(shops["kan-2"]["latest_key"], "c" * 64)
        self.assertEqual(shops["kan-2"]["verified_at"], NOW - 600)

    def test_malformed_checks_are_ignored(self):
        shops = self.run_merge({"kan-1": {"key": "../x", "at": NOW}, "kan-2": {"key": "b" * 64, "at": "now"}})
        self.assertNotIn("latest_key", shops["kan-1"])
        self.assertEqual(shops["kan-2"]["verified_at"], NOW - 86400)

    def test_missing_checks_file_keeps_the_previous_record(self):
        with mock.patch.object(sys, "argv", ["merge.py", "--checks", str(self.dir / "none.json")]):
            merge.main()
        shops = json.loads(self.file.read_text(encoding="utf-8"))["shops"]
        self.assertEqual(shops["kan-2"]["verified_at"], NOW - 86400)


class ValidateTests(unittest.TestCase):
    def test_check_fields_are_validated(self):
        import validate_data as v

        base = {"chain": "カラオケ館", "reads": ["<table><tr><td>x</td></tr></table>"] * 2,
                "url": "https://karaokekan.net/shop2price/image/1/0"}
        for fields in ({}, {"latest_key": "a" * 64, "verified_at": NOW}, {"latest_key": "missing"}):
            issues = []
            v._ocr({**base, **fields}, None, "kan-1", issues)
            self.assertEqual(issues, [], fields)
        for fields in ({"latest_key": "../x"}, {"latest_key": 1}, {"verified_at": "now"}, {"verified_at": True}):
            issues = []
            v._ocr({**base, **fields}, None, "kan-1", issues)
            self.assertTrue(issues, fields)


class WorkflowTests(unittest.TestCase):
    def test_publish_runs_even_when_no_image_changed(self):
        text = (ROOT / ".github" / "workflows" / "ocr.yml").read_text(encoding="utf-8")
        publish = text.split("\n  publish:", 1)[1].split("\n  monitor:", 1)[0]
        condition = next(line for line in publish.splitlines() if line.strip().startswith("if:"))
        self.assertIn("always()", condition)
        self.assertIn("needs.plan.result == 'success'", condition)
        self.assertNotIn("shards", condition)
        self.assertIn("--checks checks/kan_checks.json", publish)
        self.assertIn("name: checks", text.split("\n  read:", 1)[0])


if __name__ == "__main__":
    unittest.main()
