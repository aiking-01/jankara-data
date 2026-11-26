"""歌広場の料金ページの取得（scripts/update_utahiro_prices.py）と、配信への登録・検査を確かめる。通信はしない。

tests/fixtures/utahiro/uh-N.html は 2026-11-26 に保存した店のページから、営業時間・告知・料金表のタブだけを残したもの。
tests/fixtures/utahiro_prices.json はそれらを読んだ記録（変換器のテストも使う）。
"""
from __future__ import annotations

import base64
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import crawl_plan  # noqa: E402
import monitor_freshness  # noqa: E402
import net  # noqa: E402
import publish  # noqa: E402
import seen  # noqa: E402
import select_changes  # noqa: E402
import update_utahiro_prices as uh  # noqa: E402
import validate_data as v  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
RECORDS = json.loads((FIXTURES / "utahiro_prices.json").read_text(encoding="utf-8"))["shops"]
SAVED = ("uh-1", "uh-133", "uh-139", "uh-143")
NOW = 1795672800  # 2026-11-26 15:00 JST
DAY = 86400


def page(key: str) -> str:
    return (FIXTURES / "utahiro" / f"{key}.html").read_text(encoding="utf-8")


def parse(key: str, markup: str | None = None) -> dict:
    return uh.parse_page(RECORDS[key]["page"], markup if markup is not None else page(key), NOW)


def response(status: int, body: bytes = b"", headers: dict | None = None) -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result._content = body
    result.headers.update(headers or {})
    return result


class ParsePageTests(unittest.TestCase):
    def test_saved_pages_read_as_the_fixture_records(self):
        for key in SAVED:
            with self.subTest(key=key):
                self.assertEqual(parse(key), RECORDS[key])

    def test_hours_keep_the_lines_after_br(self):
        self.assertEqual(parse("uh-133")["hours"], "11:00～23:00\n金・土・祝前日：11:00～翌5:00")
        self.assertEqual(parse("uh-139")["hours"], "11:00～翌5:00\n土・日・祝日：10:00～翌5:00")
        self.assertEqual(parse("uh-143")["hours"],
                         "午前11:00～翌5:00 (金祝前11：00～翌6：00/土：午前10:00～翌6:00/日祝：午前10:00～翌5:00)")

    def test_notices_are_read_line_by_line(self):
        notices = parse("uh-133")["notices"]
        self.assertEqual(len(notices), 2)
        self.assertTrue(notices[0].startswith("・2026年12月1日(火)～2027年1月8日(金)まで、一部料金を変更"))
        self.assertEqual(notices[1], "・2026年12月18日(金)から営業時間を11時～翌5時へ変更いたします。")

    def test_price_tab_is_kept_in_page_order(self):
        record = parse("uh-1")
        self.assertEqual([band["label"] for band in record["bands"]], ["OPEN~18:00", "18:00~CLOSE", "ウタ学 アプリ会員限定プラン"])
        self.assertEqual([course["name"] for course in record["bands"][0]["courses"]], ["ソフトドリンク飲み放題", "アルコール飲み放題"])
        slot = record["bands"][0]["courses"][0]["tables"][0]
        self.assertEqual(slot, {"title": "お一人様30分料金", "header": ["", "月～金(祝日を除く)", "土・日・祝日"],
                                "rows": [["ウタ学", "160", "240"], ["一般", "200", "300"]]})
        self.assertEqual(record["bands"][0]["courses"][0]["tables"][1]["title"], "お一人様フリータイム料金\n11:00~20:00")
        self.assertEqual(record["bands"][2], {"label": "ウタ学 アプリ会員限定プラン", "courses": [],
                                              "images": ["/dcms_media/image/001_utagaku_20250901.webp"]})
        self.assertTrue(record["tail"][0].startswith("お1人様税込料金です。\n"))
        self.assertEqual(record["extras"], [])

    def test_group_only_note_stays_in_the_header(self):
        table = parse("uh-139")["bands"][0]["courses"][0]["tables"][1]
        self.assertEqual(table["header"][2], "土・日・祝日\n※2名様より受付")
        self.assertEqual(table["title"], "お一人様フリータイム料金\n11:00~20:00\n土日祝10:00～20:00")

    def soup(self, key="uh-1"):
        return BeautifulSoup(page(key), "html.parser")

    def test_hidden_block_in_the_price_tab_is_reported(self):
        soup = self.soup()
        soup.select_one("div.db-menu-tab-td")["style"] = "display: none"
        self.assertTrue(any(item.startswith("hidden") for item in parse("uh-1", str(soup))["extras"]))

    def test_unknown_headless_css_is_reported(self):
        soup = self.soup()
        hidden_css = base64.b64encode(".db-menu-tab-td{display:none}".encode()).decode()
        soup.find(id=uh.HEADLESS_ID).find("div").string = hidden_css
        self.assertIn("headless-css", parse("uh-1", str(soup))["extras"])
        self.assertTrue(uh.css_is_empty(base64.b64encode("/* 説明 */\r\n\t.hoge {\r\n\r\n\t}".encode()).decode()))

    def test_style_tag_for_the_price_tab_is_reported(self):
        markup = page("uh-1").replace("</head>", "<style>.tab1 .db-menu-tab{display:none}</style></head>")
        self.assertIn("style-tag", parse("uh-1", markup)["extras"])

    def test_second_header_and_orphan_rows_are_reported(self):
        soup = self.soup()
        header = soup.select_one("div.db-menu-tab-th")
        header.insert_after(copy.copy(header))
        self.assertTrue(any(item.startswith("second_header") for item in parse("uh-1", str(soup))["extras"]))
        soup = self.soup()
        info = soup.select("div.tab-menu-info")[1]  # 最初の表の見出し（span.in）
        info.decompose()
        self.assertTrue(any(item.startswith(("orphan_row", "second_header")) for item in parse("uh-1", str(soup))["extras"]))

    def test_page_without_price_tab_is_a_record_without_prices(self):
        record = uh.parse_page(RECORDS["uh-1"]["page"], "<html><body><p>メンテナンス中です</p></body></html>", NOW)
        self.assertEqual((record["bands"], record["hours"], record["extras"], record["checked_at"]), ([], "", [], NOW))


class FakeFetcher:
    """URL ごとに決めた応答を返す。呼ばれた時刻（偽の時計）を残す"""

    def __init__(self, answers: dict, clock):
        self.answers = answers
        self.clock = clock
        self.calls = []
        self.bytes = 0

    def get(self, url, **kwargs):
        self.calls.append((url, self.clock.now, kwargs))
        self.clock.now += 0.2
        answer = self.answers[url]
        if isinstance(answer, Exception):
            raise answer
        return answer


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class UpdateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_file = Path(directory.name) / "utahiro_prices.json"
        self.chains = {key: {"id": key, "chain": "歌広場", "url": RECORDS[key]["page"]} for key in SAVED}
        self.chains["mb-1"] = {"id": "mb-1", "chain": "メガビッグ", "url": "https://megakara.jp/shop/1/"}
        for patcher in (
            mock.patch.object(uh, "DATA_FILE", self.data_file),
            mock.patch.object(uh.update_chains, "load_previous", lambda: self.chains),
            mock.patch.object(crawl_plan.seen, "load", lambda: {"schema_version": 1, "files": {}, "tried": {}, "lists": {}}),
            mock.patch.object(crawl_plan, "recording", lambda: False),
            # 間引き（crawl_plan.py）は別に確かめる。ここでは全店を取る
            mock.patch.dict("os.environ", {"CRAWL_ALL": "1"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.clock = Clock()
        self.log = []

    def run_update(self, answers: dict) -> tuple[bool, FakeFetcher]:
        fetcher = FakeFetcher(answers, self.clock)
        ok = uh.update(log=self.log.append, now=NOW, fetcher=fetcher, sleep=self.clock.sleep, clock=self.clock)
        return ok, fetcher

    def saved(self) -> dict:
        return json.loads(self.data_file.read_text(encoding="utf-8"))

    def previous(self, shops: dict) -> None:
        self.data_file.write_text(json.dumps({"schema_version": 1, "updated_at": 5, "shops": shops}, ensure_ascii=False),
                                  encoding="utf-8")

    def ok_answers(self) -> dict:
        return {RECORDS[key]["page"]: response(200, page(key).encode("utf-8")) for key in SAVED}

    def test_pages_are_saved_one_at_a_time_with_a_pause(self):
        ok, fetcher = self.run_update(self.ok_answers())
        self.assertTrue(ok)
        doc = self.saved()
        self.assertEqual(doc["updated_at"], NOW)
        self.assertEqual(doc["shops"], {key: RECORDS[key] for key in SAVED})
        starts = [at for _, at, _ in fetcher.calls]
        self.assertEqual(len(starts), 4)
        self.assertTrue(all(later - earlier >= uh.INTERVAL for earlier, later in zip(starts, starts[1:])), starts)
        self.assertTrue(all(kwargs["html"] and not kwargs["allow_redirects"] for _, _, kwargs in fetcher.calls))

    def test_network_failure_keeps_the_previous_record_and_its_date(self):
        old = dict(RECORDS["uh-1"], checked_at=100)
        self.previous({"uh-1": old})
        answers = self.ok_answers()
        answers[RECORDS["uh-1"]["page"]] = requests.ConnectionError("timed out")
        ok, _ = self.run_update(answers)
        self.assertFalse(ok)
        doc = self.saved()
        self.assertEqual(doc["shops"]["uh-1"], old)
        self.assertEqual(doc["updated_at"], 5)
        self.assertEqual(doc["shops"]["uh-133"]["checked_at"], NOW)

    def test_server_error_keeps_the_previous_record(self):
        old = dict(RECORDS["uh-1"], checked_at=100)
        self.previous({"uh-1": old})
        answers = self.ok_answers()
        answers[RECORDS["uh-1"]["page"]] = response(500, b"<html></html>")
        self.run_update(answers)
        self.assertEqual(self.saved()["shops"]["uh-1"], old)

    def test_closed_or_moved_page_becomes_a_record_without_prices(self):
        self.previous({"uh-1": dict(RECORDS["uh-1"], checked_at=100), "uh-133": dict(RECORDS["uh-133"], checked_at=100)})
        answers = self.ok_answers()
        answers[RECORDS["uh-1"]["page"]] = response(404, b"<html>not found</html>")
        answers[RECORDS["uh-133"]["page"]] = response(302, b"", {"Location": "https://www.utahiro.com/shops/"})
        ok, _ = self.run_update(answers)
        self.assertTrue(ok)
        shops = self.saved()["shops"]
        for key, why in (("uh-1", "HTTP 404"), ("uh-133", "HTTP 302 https://www.utahiro.com/shops/")):
            self.assertEqual((shops[key]["bands"], shops[key]["missing"], shops[key]["checked_at"]), ([], why, NOW))

    def test_page_without_table_is_saved_without_prices(self):
        self.previous({"uh-1": dict(RECORDS["uh-1"], checked_at=100)})
        answers = self.ok_answers()
        answers[RECORDS["uh-1"]["page"]] = response(200, "<html><body>準備中</body></html>".encode("utf-8"))
        self.run_update(answers)
        record = self.saved()["shops"]["uh-1"]
        self.assertEqual((record["bands"], record["checked_at"]), ([], NOW))

    def test_page_that_cannot_be_read_is_saved_without_prices(self):
        self.previous({"uh-1": dict(RECORDS["uh-1"], checked_at=100)})
        with mock.patch.object(uh, "price_area", side_effect=AttributeError("壊れたページ")):
            ok, _ = self.run_update(self.ok_answers())
        self.assertTrue(ok)
        record = self.saved()["shops"]["uh-1"]
        self.assertEqual((record["bands"], record["missing"], record["checked_at"]),
                         ([], "読めないページ (AttributeError)", NOW))

    def test_throttled_crawl_stops_and_keeps_the_rest(self):
        old = {key: dict(RECORDS[key], checked_at=100) for key in SAVED}
        self.previous(old)
        answers = self.ok_answers()
        first = RECORDS["uh-1"]["page"]
        answers[first] = response(429, b"")
        ok, fetcher = self.run_update(answers)
        self.assertFalse(ok)
        self.assertEqual([url for url, _, _ in fetcher.calls], [first])
        self.assertEqual(self.saved()["shops"], old)

    def test_deadline_saves_what_was_fetched(self):
        self.previous({})
        answers = self.ok_answers()
        answers[RECORDS["uh-139"]["page"]] = net.OutOfTime("締め切り")
        ok, fetcher = self.run_update(answers)
        self.assertFalse(ok)
        self.assertEqual(len(fetcher.calls), 3)
        self.assertEqual(sorted(self.saved()["shops"]), ["uh-1", "uh-133"])

    def test_closed_shops_are_forgotten_and_other_chains_ignored(self):
        self.previous({"uh-999": dict(RECORDS["uh-1"], checked_at=100)})
        _, fetcher = self.run_update(self.ok_answers())
        self.assertNotIn("uh-999", self.saved()["shops"])
        self.assertNotIn("https://megakara.jp/shop/1/", [url for url, _, _ in fetcher.calls])

    def test_shop_url_outside_the_official_site_is_not_fetched(self):
        self.chains["uh-1"] = {"id": "uh-1", "chain": "歌広場", "url": "https://example.com/shops/st_x/1"}
        _, fetcher = self.run_update(self.ok_answers())
        self.assertNotIn("https://example.com/shops/st_x/1", [url for url, _, _ in fetcher.calls])

    def test_a_third_of_the_shops_are_fetched_each_day_oldest_first(self):
        self.previous({key: dict(RECORDS[key], checked_at=NOW - (4 + i) * DAY) for i, key in enumerate(SAVED)})
        with mock.patch.dict("os.environ", {"CRAWL_ALL": ""}):
            _, fetcher = self.run_update(self.ok_answers())
        self.assertEqual([url for url, _, _ in fetcher.calls], [RECORDS["uh-143"]["page"], RECORDS["uh-139"]["page"]])

    def test_new_shops_are_added_a_little_at_a_time_after_the_first_run(self):
        listed = {f"uh-{i}": {} for i in range(60)}
        self.assertEqual(len(uh.candidates(listed, {})), 60)
        previous = {f"uh-{i}": {} for i in range(8)}
        chosen = uh.candidates(listed, previous)
        # validate_data.py は前回の 1.25 倍 + 10 店を超えるとチェーンごと戻すので、新しい店は 8 // 4 + 10 店まで
        self.assertEqual(len(chosen), 8 + 12)
        self.assertLessEqual(len(chosen), 8 * 1.25 + 10)


class RegistrationTests(unittest.TestCase):
    """取得した元データが、確認日時・変更の選別・公開・鮮度の確認・検査のすべてに登録されていること"""

    def test_every_step_knows_the_source(self):
        self.assertEqual(seen.TRACKED["utahiro"], "source/utahiro_prices.json")
        self.assertIn("source/utahiro_prices.json", select_changes.FILES)
        self.assertEqual(publish.SOURCES["utahiro"], ROOT / "build" / "utahiro.json")
        self.assertIn("utahiro", monitor_freshness.OPTIONAL)
        self.assertIn("utahiro", monitor_freshness.REFRESHED)
        self.assertEqual(v.RAW["utahiro_prices"], "source/utahiro_prices.json")
        self.assertEqual(v.BUILT["utahiro"], "build/utahiro.json")
        self.assertEqual(v.SOURCE_OF_BUILD["utahiro"], "utahiro_prices")
        self.assertEqual(uh.SEEN_SOURCE, seen.TRACKED["utahiro"])

    def test_refresh_workflow_runs_the_crawl_like_the_other_chains(self):
        workflow = (ROOT / ".github" / "workflows" / "refresh.yml").read_text(encoding="utf-8")
        step = workflow.split("- name: Update Utahiroba prices", 1)[1].split("- name:", 1)[0]
        for line in ("continue-on-error: true", "timeout-minutes: 75", "HTTPS_PROXY: ${{ env.CRAWL_PROXY }}",
                     "run: python scripts/update_utahiro_prices.py"):
            self.assertIn(line, step)
        self.assertLess(workflow.index("Update Utahiroba prices"), workflow.index("- name: Publish data"))

    def test_shop_not_checked_for_two_weeks_is_not_published(self):
        document = {"updated_at": 0, "shops": {"uh-1": {"checked_at": NOW - DAY}, "uh-2": {"checked_at": NOW - 15 * DAY}}}
        kept, latest = publish.without_stale("utahiro", document, {"files": {}}, NOW)
        self.assertEqual(list(kept["shops"]), ["uh-1"])
        self.assertEqual(latest, NOW - DAY)

    def test_no_shop_to_serve_leaves_the_chain_out_of_the_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            entry, ok = publish.publish_file("utahiro", {"updated_at": 0, "shops": {}}, None, Path(directory),
                                             trust_count=True)
        self.assertEqual((entry, ok), (None, True))


class ValidateTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, v, "REPAIR", False)
        self.addCleanup(v.QUARANTINE.clear)
        self.addCleanup(v.REPAIRED.clear)

    def raw_issues(self, record, old=None, key="uh-1"):
        issues = []
        v._utahiro_raw(record, old, key, issues)
        return issues

    def test_saved_records_and_records_without_prices_pass(self):
        for key, record in RECORDS.items():
            with self.subTest(key=key):
                self.assertEqual(self.raw_issues(record, record, key), [])
        missing = {"page": RECORDS["uh-1"]["page"], "checked_at": NOW, "hours": "", "notices": [], "bands": [],
                   "tail": [], "extras": [], "missing": "HTTP 404"}
        self.assertEqual(self.raw_issues(missing, RECORDS["uh-1"]), [])

    def test_html_bad_url_huge_amounts_and_jumps_are_reported(self):
        record = copy.deepcopy(RECORDS["uh-1"])
        record["hours"] = "<div>11:00～翌5:00</div>"
        record["page"] = "https://example.com/shops/st_kawasaki/1"
        record["bands"][0]["courses"][0]["tables"][0]["rows"][1][1] = "99,999"
        issues = self.raw_issues(record)
        self.assertTrue(any("営業時間" in issue for issue in issues), issues)
        self.assertTrue(any("URL" in issue for issue in issues), issues)
        self.assertTrue(any("巨大な金額" in issue for issue in issues), issues)
        jumped = copy.deepcopy(RECORDS["uh-1"])
        jumped["bands"][0]["courses"][0]["tables"][1]["rows"][1][1] = "9,800"  # 1,250 → 9,800
        self.assertTrue(any("急騰" in issue for issue in self.raw_issues(jumped, RECORDS["uh-1"])))

    def test_shop_issues_point_at_the_shop(self):
        records = {"uh-1": {}}
        self.assertEqual(v._issue_key("utahiro_prices/uh-1: 料金表URLが店舗一覧と一致しません", "utahiro_prices", records), "uh-1")
        self.assertEqual(v._issue_key("uh-1.bands[0]: 帯の形式が不正", "utahiro_prices", records), "uh-1")
        self.assertEqual(v._issue_key("build/utahiro/uh-1: 店舗一覧に存在しない店舗の料金", "utahiro_prices", records), "uh-1")

    def test_published_week_is_checked(self):
        week = {key: [660, 1740] for key in v.WEEK_KEYS}
        issues = []
        v._utahiro_built({"week": week}, "uh-1", issues)
        self.assertEqual(issues, [])
        for broken in ({k: x for k, x in week.items() if k != "祝前"}, {**week, "土": [1740, 660]}, {**week, "日": [0, 3000]}):
            issues = []
            v._utahiro_built({"week": broken}, "uh-1", issues)
            self.assertEqual(len(issues), 1)

    def test_first_run_without_the_source_file_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git = lambda *args: subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)  # noqa: E731
            git("init", "-q")
            git("config", "user.email", "t@example.com")
            git("config", "user.name", "t")
            shop = {"id": "uh-1", "chain": "歌広場", "name": "店", "prefecture": "東京都", "address": "東京都新宿区1",
                    "lat": 35.6, "lng": 139.7, "url": RECORDS["uh-1"]["page"]}
            for name, file in v.RAW.items():
                if name == "utahiro_prices":
                    continue
                shops = [shop] if name == "chains" else ([{"id": 1}] if name == "prices" else {})
                path = root / file
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"schema_version": 1, "updated_at": 1, "shops": shops}), encoding="utf-8")
            git("add", "-A")
            git("commit", "-qm", "base")
            with mock.patch.object(v, "ROOT", root):
                issues, summary = v.run_validation("HEAD", "sources")
                self.assertEqual(issues, [])
                self.assertIn("utahiro_prices: 0 件 (変更 0 件)", summary)
                # 店舗一覧にない店の記録は、その店の異常になる（チェーン全体を止めない）
                (root / v.RAW["utahiro_prices"]).write_text(json.dumps({"schema_version": 1, "updated_at": 2, "shops": {
                    "uh-1": RECORDS["uh-1"], "uh-2": dict(RECORDS["uh-1"], page="https://www.utahiro.com/shops/st_x/2")}},
                    ensure_ascii=False), encoding="utf-8")
                issues, _ = v.run_validation("HEAD", "sources")
        self.assertEqual(issues, ["utahiro_prices/uh-2: 店舗一覧にない歌広場の店"])
        self.assertEqual(v.ISSUE_SOURCES[:1], ["utahiro_prices"])


if __name__ == "__main__":
    unittest.main()
