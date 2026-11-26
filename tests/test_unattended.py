"""人の確認なしで回すための仕組み（店ごとの確認日時、異常の修復、古い金額を配らない）を確かめる。"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import publish  # noqa: E402
import seen  # noqa: E402
import validate_data as v  # noqa: E402

DAY = 86400


def write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


class SeenTests(unittest.TestCase):
    def test_mark_keeps_the_newest_date_and_forgets_closed_shops(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_file = root / "source" / "seen.json"
            write(root / "source" / "chain_prices.json",
                  {"updated_at": 1, "shops": {"be-1": {"checked_at": 100}, "be-2": {"checked_at": 50}}})
            seen.save({"schema_version": 1, "files": {"source/chain_prices.json": {"be-1": 90, "be-2": 80, "be-9": 70}}},
                      state_file)
            state = seen.mark(root, state_file)
            self.assertEqual(state["files"]["source/chain_prices.json"], {"be-1": 100, "be-2": 80})

    def test_jankara_list_uses_shop_ids(self):
        block = seen.block_for({"shops": [{"id": 4, "checked_at": 10}]}, {})
        self.assertEqual(block, {"4": 10})


class StaleTests(unittest.TestCase):
    def test_shop_not_checked_for_two_weeks_is_not_published(self):
        now = 100 * DAY
        state = {"files": {"source/chain_prices.json": {"be-1": now - DAY, "be-2": now - 15 * DAY}}}
        document = {"updated_at": 0, "shops": {"be-1": {"checked_at": 0}, "be-2": {"checked_at": 0}}}
        kept, latest = publish.without_stale("bigecho", document, state, now)
        self.assertEqual(list(kept["shops"]), ["be-1"])
        self.assertEqual(latest, now - DAY)

    def test_shop_checked_in_its_own_record_counts_when_seen_is_missing(self):
        now = 100 * DAY
        document = {"updated_at": 0, "shops": {"mn-4": {"checked_at": now - 2 * DAY}, "mn-5": {"checked_at": 0}}}
        kept, _ = publish.without_stale("maneki", document, {"files": {}}, now)
        self.assertEqual(list(kept["shops"]), ["mn-4"])

    def test_jankara_shops_stay_because_the_file_is_also_the_shop_list(self):
        now = 100 * DAY
        document = {"updated_at": 0, "shops": [{"id": 4, "checked_at": now - 2 * DAY}, {"id": 5, "checked_at": 0}]}
        kept, latest = publish.without_stale("jankara", document, {"files": {}}, now)
        self.assertEqual([shop["id"] for shop in kept["shops"]], [4, 5])
        self.assertEqual(latest, now - 2 * DAY)

    def test_files_outside_the_daily_crawl_are_left_alone(self):
        document = {"updated_at": 0, "shops": {"kan-1": {"checked_at": 0}}}
        kept, latest = publish.without_stale("kan", document, {"files": {}}, 100 * DAY)
        self.assertEqual(kept, document)
        self.assertEqual(latest, 0)

    def test_checked_count_is_published_even_when_conversion_drops_shops(self):
        # 期間限定の料金で変換が読まなくなった店は、前回の金額を残さずに外す
        old = {"count": 77, "checked_at": 0, "sha256": "x"}
        with tempfile.TemporaryDirectory() as directory:
            entry, ok = publish.publish_file("cotedazur", {"updated_at": 0, "shops": {}}, old, Path(directory),
                                             trust_count=True)
            self.assertTrue(ok)
            self.assertEqual(entry["count"], 0)
            kept, ok = publish.publish_file("cotedazur", {"updated_at": 0, "shops": {}}, old, Path(directory))
            self.assertFalse(ok)
            self.assertIs(kept, old)


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, v, "REPAIR", False)

    def test_many_changed_shops_is_only_a_warning_when_repairing(self):
        old = {"shops": {f"be-{i}": {"sections": [], "hours": "9:00"} for i in range(100)}}
        new = {"shops": {f"be-{i}": {"sections": [], "hours": "10:00"} for i in range(100)}}
        v.REPAIR = True
        v.WARNINGS.clear()
        issues = []
        v._validate_doc("chain_prices", new, old, issues)
        self.assertEqual(issues, [])
        self.assertTrue(any("35%" in warning for warning in v.WARNINGS))

    def test_published_side_may_shrink_or_empty_when_repairing(self):
        v.REPAIR = True
        issues = []
        v._validate_doc("cotedazur", {"shops": {}}, {"shops": {f"cd-{i}": {} for i in range(77)}}, issues, built=True)
        self.assertEqual(issues, [])

    def test_issue_key_reads_shop_ids_in_every_form(self):
        records = {"be-1": {}, "cd-10101": {}}
        self.assertEqual(v._issue_key("be-1: ビッグエコーの料金表が大量欠落", "chain_prices", records), "be-1")
        self.assertEqual(v._issue_key("build/bigecho/be-1: 店舗一覧に存在しない店舗の料金", "chain_prices", records), "be-1")
        self.assertEqual(v._issue_key("cd-10101.price_plan[0]: 金額が不正", "cotedazur_prices", records), "cd-10101")
        self.assertEqual(v._issue_key("be-1.sections[2]: 行列の形が不正", "chain_prices", records), "be-1")
        self.assertIsNone(v._issue_key("chain_prices: 店舗数が異常に減少 (444 → 10)", "chain_prices", records))

    def test_repair_drops_bad_shops_and_reverts_a_broken_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git = lambda *args: subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
            git("init", "-q")
            git("config", "user.email", "t@example.com")
            git("config", "user.name", "t")
            be = root / "source" / "chain_prices.json"
            mn = root / "source" / "maneki_prices.json"
            state_file = root / "source" / "seen.json"
            write(be, {"updated_at": 1, "shops": {"be-1": {"checked_at": 1}, "be-2": {"checked_at": 1}}})
            write(mn, {"updated_at": 1, "shops": {"mn-1": {"checked_at": 1}, "mn-2": {"checked_at": 1}}})
            seen.save({"schema_version": 1, "files": {"source/maneki_prices.json": {"mn-1": 1, "mn-2": 1}}}, state_file)
            git("add", "-A")
            git("commit", "-qm", "base")
            write(be, {"updated_at": 2, "shops": {"be-1": {"checked_at": 2}, "be-2": {"checked_at": 2}}})
            write(mn, {"updated_at": 2, "shops": {"mn-1": {"checked_at": 2}}})
            seen.save({"schema_version": 1, "files": {"source/chain_prices.json": {"be-1": 2, "be-2": 2},
                                                      "source/maneki_prices.json": {"mn-1": 2}}}, state_file)
            issues = ["be-2: ビッグエコーの料金表が大量欠落", "maneki_prices: 店舗数が異常に減少 (2 → 1)"]
            with mock.patch.object(v, "ROOT", root), mock.patch.object(seen, "SEEN_FILE", state_file):
                v.ISSUE_SOURCES[:] = ["chain_prices", "maneki_prices"]
                actions = v.repair("HEAD", issues)
            self.assertEqual(len(actions), 2)
            self.assertEqual(list(json.loads(be.read_text(encoding="utf-8"))["shops"]), ["be-1"])
            self.assertEqual(json.loads(mn.read_text(encoding="utf-8"))["updated_at"], 1)
            state = seen.load(state_file)
            # 外した店は確かめていない扱い、戻したチェーンは前回の日時のまま
            self.assertEqual(state["files"]["source/chain_prices.json"], {"be-1": 2})
            self.assertEqual(state["files"]["source/maneki_prices.json"], {"mn-1": 1, "mn-2": 1})


class RateLimitTests(unittest.TestCase):
    def test_429_waits_and_reads_the_shop_again(self):
        import update_prices
        calls = []

        class Response:
            def __init__(self, status, headers=None):
                self.status_code, self.headers, self.text, self.encoding = status, headers or {}, "<html></html>", None

        responses = [Response(429, {"Retry-After": "7"}), Response(200)]
        session = mock.Mock()
        session.get.side_effect = lambda *args, **kwargs: responses.pop(0)
        with mock.patch.object(update_prices, "rate_limit_waited", 0),                 mock.patch.object(update_prices.time, "sleep", side_effect=calls.append):
            self.assertEqual(update_prices.fetch(session, 4, None), "<html></html>")
        self.assertEqual(calls, [7])

    def test_429_beyond_the_wait_budget_stops_the_crawl(self):
        import update_prices
        session = mock.Mock()
        session.get.return_value = mock.Mock(status_code=429, headers={})
        with mock.patch.object(update_prices, "rate_limit_waited", update_prices.RATE_LIMIT_BUDGET),                 mock.patch.object(update_prices.time, "sleep"):
            with self.assertRaises(update_prices.RateLimited):
                update_prices.fetch(session, 4, None)


if __name__ == "__main__":
    unittest.main()
