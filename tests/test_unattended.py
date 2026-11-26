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
import crawl_plan  # noqa: E402

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
            (Path(directory) / "cotedazur.json.xz").write_bytes(b"old")
            entry, ok = publish.publish_file("cotedazur", {"updated_at": 0, "shops": {}}, old, Path(directory),
                                             trust_count=True)
            # アプリは count 0 の項目があると manifest 全体を読まないので、載せずにファイルも消す
            self.assertTrue(ok)
            self.assertIsNone(entry)
            self.assertFalse((Path(directory) / "cotedazur.json.xz").exists())
            kept, ok = publish.publish_file("cotedazur", {"updated_at": 0, "shops": {}}, old, Path(directory))
            self.assertFalse(ok)
            self.assertIs(kept, old)


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, v, "REPAIR", False)
        self.addCleanup(v.QUARANTINE.clear)
        self.addCleanup(v.REPAIRED.clear)

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
            # 外した店は最後に通った記録と一緒に隔離し、翌日戻ってきたらそれと比べる
            self.assertEqual(v.QUARANTINE["chain_prices"], {"be-2": {"checked_at": 1}})


class QuarantineTests(unittest.TestCase):
    """修復で外した店は、翌日戻ってきても比べる相手のない新しい店として素通しさせない。"""

    def setUp(self):
        self.addCleanup(setattr, v, "REPAIR", False)
        self.addCleanup(v.QUARANTINE.clear)
        self.addCleanup(v.REPAIRED.clear)

    @staticmethod
    def shop(tables):
        return {"sections": [{"header": ["時間", "一般"], "rows": [["30分", "300円"]]}] * tables, "checked_at": 1}

    def test_returning_shop_is_compared_with_its_last_good_record(self):
        v.REPAIR = True
        v.QUARANTINE["chain_prices"] = {"be-1": self.shop(10)}
        issues = []
        v._validate_doc("chain_prices", {"shops": {"be-1": self.shop(2), "be-2": self.shop(10)}},
                        {"shops": {"be-2": self.shop(10)}}, issues)
        self.assertTrue(any(issue.startswith("be-1: ") for issue in issues), issues)

    def test_shops_left_out_by_the_repair_do_not_count_as_a_lost_chain(self):
        v.REPAIR = True
        v.REPAIRED["chain_prices"] = {f"be-{i}" for i in range(80, 100)}
        issues = []
        old = {"shops": {f"be-{i}": self.shop(1) for i in range(100)}}
        new = {"shops": {f"be-{i}": self.shop(1) for i in range(80)}}
        v._validate_doc("chain_prices", new, old, issues)
        self.assertEqual(issues, [])

    def test_a_few_closed_shops_in_a_small_chain_are_not_an_anomaly(self):
        issues = []
        v._validate_doc("megabig_prices", {"shops": {f"mb-{i}": {} for i in range(10)}},
                        {"shops": {f"mb-{i}": {} for i in range(11)}}, issues)
        self.assertFalse(any("店舗数" in issue for issue in issues), issues)

    def test_price_source_may_become_empty(self):
        issues = []
        v._validate_doc("banban_prices", {"shops": {}}, {"shops": {"bb-1": {}}}, issues)
        self.assertFalse(any("データが空" in issue for issue in issues))

    def test_published_side_may_grow_or_shrink_without_stopping(self):
        # 変換を直して読める店が増えた（カラオケ館 82 → 121）、期間の告知で 0 店になった（JOYSOUND）
        for repair in (False, True):
            v.REPAIR = repair
            issues = []
            v._validate_doc("kan", {"shops": {f"kan-{i}": {} for i in range(121)}},
                            {"shops": {f"kan-{i}": {} for i in range(82)}}, issues, built=True)
            v._validate_doc("joysound", {"shops": {}}, {"shops": {f"js-{i}": {} for i in range(61)}}, issues, built=True)
            # 店ごとの中身の検査（空の料金など）は別。店数と変更の割合では止めない
            self.assertFalse([issue for issue in issues if "店舗数" in issue or "35%" in issue], issues)

    def test_shops_left_out_in_the_sources_stage_do_not_count_as_a_drop_later(self):
        # full の段階は別のプロセスなので、sources の段階で隔離した店を数に戻す
        v.QUARANTINE["joyjoy_prices"] = {f"jj-{i}": {} for i in range(10)}
        issues = []
        v._validate_doc("joyjoy_prices", {"shops": {f"jj-{i}": {} for i in range(10, 100)}},
                        {"shops": {f"jj-{i}": {} for i in range(100)}}, issues)
        self.assertFalse(any("店舗数" in issue for issue in issues), issues)

    def test_closed_shops_do_not_count_as_a_drop(self):
        self.addCleanup(v.CURRENT_SHOPS.clear)
        v.CURRENT_SHOPS.update(f"jj-{i}" for i in range(90))
        issues = []
        v._validate_doc("joyjoy_prices", {"shops": {f"jj-{i}": {} for i in range(90)}},
                        {"shops": {f"jj-{i}": {} for i in range(100)}}, issues)
        self.assertFalse(any("店舗数" in issue for issue in issues), issues)

    def test_quarantine_releases_shops_that_passed_and_keeps_the_rest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root / "source" / "chain_prices.json", {"updated_at": 1, "shops": {"be-1": self.shop(1)}})
            v.QUARANTINE.update({"chain_prices": {"be-1": self.shop(9), "be-2": self.shop(9)}})
            with mock.patch.object(v, "ROOT", root):
                v.save_quarantine()
                saved = json.loads((root / v.QUARANTINE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(list(saved["sources"]["chain_prices"]), ["be-2"])


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


class CrawlPlanTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict("os.environ", {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        import os
        os.environ.pop("CRAWL_ALL", None)

    def plan(self, last, **kwargs):
        now = 100 * DAY
        state = {"files": {"source/x.json": {key: now - age for key, age in last.items()}}}
        return crawl_plan.due(list(last), lambda key: {}, "source/x.json", now, state=state, **kwargs)

    def test_a_third_of_the_shops_oldest_first(self):
        last = {f"s{i}": (i + 1) * DAY for i in range(6)}  # s5 が最も古い
        self.assertEqual(self.plan(last), ["s5", "s4"])

    def test_shops_checked_within_20_hours_are_skipped(self):
        self.assertEqual(self.plan({"a": 3600, "b": 3600, "c": 3600}), [])

    def test_late_shops_are_fetched_beyond_the_quota(self):
        last = {f"s{i}": 8 * DAY for i in range(6)}
        self.assertEqual(len(self.plan(last)), 6)

    def test_new_and_forced_shops_are_always_fetched(self):
        now = 100 * DAY
        state = {"files": {"source/x.json": {"a": now - DAY, "b": now - DAY, "c": now - DAY}}}
        due = crawl_plan.due(["a", "b", "c", "new"], lambda key: {}, "source/x.json", now, state=state,
                             force=lambda key: key == "b")
        self.assertEqual(due[:2], ["b", "new"])

    def test_crawl_all_fetches_every_shop(self):
        import os
        os.environ["CRAWL_ALL"] = "1"
        self.assertEqual(len(self.plan({f"s{i}": 2 * DAY for i in range(6)})), 6)

    def test_jankara_reads_every_number_only_on_sunday(self):
        import update_prices
        previous = {4: {"checked_at": 1}, 5: {"checked_at": 1, "closed": True}}
        sunday = 1793491200  # 2026-11-01 00:00 UTC は日曜
        monday = sunday + DAY
        self.assertIsNone(update_prices.numbers_today(previous, sunday))
        with mock.patch.object(crawl_plan.seen, "load", return_value={"files": {}}),                 mock.patch.object(crawl_plan.seen, "save") as save,                 mock.patch.object(crawl_plan, "recording", return_value=True):
            self.assertEqual(update_prices.numbers_today(previous, monday), [4])
        save.assert_called_once()

    def test_shops_that_keep_failing_are_retried_less_often(self):
        now = 100 * DAY
        ids = [f"s{i}" for i in range(3)]
        state = {"files": {"source/x.json": {key: now - 10 * DAY for key in ids}}, "tried": {}}
        # 1 回目: 全店を試す（10 日遅れ）
        self.assertEqual(sorted(crawl_plan.due(ids, lambda key: {}, "source/x.json", now, state=state)), ids)
        # s0 だけ確かめられた。s1, s2 は失敗が 1 回になる
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root / "source/x.json", {"shops": {"s0": {"checked_at": now + 60}, "s1": {}, "s2": {}}})
            path = root / "seen.json"
            seen.save(state, path)
            with mock.patch.dict(seen.TRACKED, {"x": "source/x.json"}, clear=True):
                state = seen.mark(root, path)
        self.assertEqual(state["tried"]["source/x.json"], {"s1": [now, 1], "s2": [now, 1]})
        # 次の日の同じ時刻の少し前でも、1 日の間隔を置いて取り直す。直後は取らない
        self.assertEqual(crawl_plan.due(ids, lambda key: {}, "source/x.json", now + 3600, state=state), [])
        self.assertEqual(sorted(crawl_plan.due(ids, lambda key: {}, "source/x.json", now + DAY - 3600, state=state)),
                         ["s1", "s2"])

    def test_shop_list_is_fetched_again_after_six_days(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seen.json"
            with mock.patch.object(seen, "SEEN_FILE", path), mock.patch.object(sys, "argv", ["seen.py", "list-due", "chains", "6"]):
                self.assertEqual(seen.main(), 0)
                sys.argv[1:] = ["list-done", "chains"]
                self.assertEqual(seen.main(), 0)
                sys.argv[1:] = ["list-due", "chains", "6"]
                self.assertEqual(seen.main(), 1)


if __name__ == "__main__":
    unittest.main()
