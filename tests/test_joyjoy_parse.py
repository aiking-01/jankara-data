"""JOYJOY の料金表の読み取りと、取り込まない判定のテスト。tests/fixtures の見本だけを読む。"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import net  # noqa: E402
import update_joyjoy_prices as u  # noqa: E402
import validate_data as v  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PAGE = (FIXTURES / "joyjoy_store.html").read_text(encoding="utf-8")
URL = "https://www.joyjoy.co.jp/store/view/Sample-Store"
NOW = 1795600000  # 2026-11-26 (JST) ごろ

DAY_PRICE_ROW = """<td class="time">AM7:00～AM12:00<br>AM12:00～PM7:00</td>
							<td>￥135～<br>￥160～</td>
						</tr>"""
FREE_SCHEDULE = "<td>日～木/祝日<br>AM0:00～AM5:00<br>金土/祝前/特日<br>AM1:00～AM6:00</td>"
NOTICE_AREA = "最新カラオケ機種 導入しました♪<br />"


def page(old: str = "", new: str = "", count: int = 1) -> str:
    if not old:
        return PAGE
    assert old in PAGE, old
    return PAGE.replace(old, new, count)


def with_notice(text: str) -> str:
    return page(NOTICE_AREA, NOTICE_AREA + text + "<br />")


def between_tables(markup: str) -> str:
    """DAY TIME と NIGHT TIME の表の間に markup を入れる"""
    return re.sub(r"</table>\s*<table", lambda _: f"</table>\n{markup}\n<table", PAGE, count=1)


def tables() -> list[str]:
    return re.findall(r'<table[^>]*class="price_info_list">.*?</table>', PAGE, flags=re.S)


class ParseTests(unittest.TestCase):
    def test_sample_page_is_read(self):
        record = u.parse_page(URL, PAGE, NOW)
        self.assertEqual(record["page"], URL)
        self.assertEqual(record["checked_at"], NOW)
        self.assertEqual(record["note"], u.NOTE)
        self.assertNotIn("notices", record)  # 「繁忙期は料金が異なる」は注記と同じ意味なので告知に数えない
        rates = {rate["period"]: rate for rate in record["rates"]}
        self.assertEqual([w["minimum_yen"] for w in rates["daytime"]["windows"]], [135, 160])
        self.assertEqual(rates["nighttime"]["windows"], [
            {"hours": "PM7:00~AM7:00", "minimum_yen": 295, "price_text": "¥295~"}])
        self.assertEqual([(w["days"], w["hours"], w["minimum_yen"]) for w in rates["freetime"]["windows"]],
                         [("日~木/祝日", "AM0:00~AM5:00", 1180), ("金土/祝前/特日", "AM1:00~AM6:00", 1180)])
        self.assertEqual(rates["freetime"]["charge"], "お一人様 室料")
        issues = []
        v._joyjoy(record, None, "jj-Sample-Store", issues, require_checked_at=True)
        self.assertEqual(issues, [])

    def test_known_layouts_of_other_shops_are_read(self):
        cases = {
            # プラン名を 1 行で書く店と、2 行で書く店
            "<td>夜3時間フリー （ジュース飲み放題限定）夜4時間フリー</td>":
                [(None, "夜3時間フリー (ジュース飲み放題限定)夜4時間フリー")],
            "<td>夜3時間フリー<br>（ジュース飲み放題限定）夜4時間フリー</td>":
                [(None, "夜3時間フリー / (ジュース飲み放題限定)夜4時間フリー")],
            "<td>AM0:00～AM6:00</td>": [(None, "AM0:00~AM6:00")],
            "<td>金土/祝前/特日<br>AM1:00～AM6:00</td>": [("金土/祝前/特日", "AM1:00~AM6:00")],
        }
        for cell, expected in cases.items():
            record = u.parse_page(URL, page(FREE_SCHEDULE, cell), NOW)
            free = next(rate for rate in record["rates"] if rate["period"] == "freetime")
            self.assertEqual([(w.get("days"), w["hours"]) for w in free["windows"]], expected, cell)
        record = u.parse_page(URL, page("<td>PM7:00～AM7:00</td>", "<td>PM7:00～AM1:00　(金土祝前日はAM3:00迄)</td>"), NOW)
        night = next(rate for rate in record["rates"] if rate["period"] == "nighttime")
        self.assertEqual(night["windows"][0]["hours"], "PM7:00~AM1:00 (金土祝前日はAM3:00迄)")
        described = "☆夜3時間フリーはジュース･カクテル・サワー・ハイボール各種飲み放題付き　※土曜・特日を除く"
        record = u.parse_page(URL, page("<td colspan=\"3\">☆ジュース･カクテル･サワー・ハイボール各種飲み放題付き</td>\n"
                                        "\t\t\t\t\t\t\t</tr>\n\t\t\t\t\t</table>\n\t\t\t\t\t<p",
                                        f"<td colspan=\"3\">{described}</td>\n\t\t\t\t\t\t\t</tr>\n\t\t\t\t\t</table>\n"
                                        "\t\t\t\t\t<p"), NOW)
        self.assertEqual(record["rates"][2]["included"],
                         ["☆夜3時間フリーはジュース・カクテル・サワー・ハイボール各種飲み放題付き ※土曜・特日を除く"])

    def assertRejected(self, markup: str, message: str):
        with self.assertRaises(ValueError) as caught:
            u.parse_page(URL, markup, NOW)
        self.assertIn(message, str(caught.exception))

    def test_extra_price_row_is_not_taken(self):
        # 学生などの行が足されると「最低」が誤りになりうるので、店ごと取り込まない
        extra = DAY_PRICE_ROW + """
						<tr>
							<th><small>学生様</small><br>30分</th>
							<td>AM7:00～AM12:00</td>
							<td>￥100～</td>
						</tr>"""
        self.assertRejected(page(DAY_PRICE_ROW, extra), "行の数")

    def test_unknown_row_heading_or_price_is_not_taken(self):
        self.assertRejected(page("30分</th>", "60分</th>"), "料金行の見出し")
        self.assertRejected(page("室料</th>", "会員 室料</th>"), "料金行の見出し")
        self.assertRejected(page("<td>￥295～</td>", "<td>￥295</td>"), "最低価格を読み取れません")
        self.assertRejected(page("<td>￥295～</td>", "<td>￥295～<br>（税別）</td>"), "一致しません")
        self.assertRejected(page("<td>￥295～</td>", "<td>￥29～</td>"), "不自然")
        self.assertRejected(page("<td>￥1,180～</td>", "<td>￥118～</td>"), "不自然")
        self.assertRejected(page("<td>PM7:00～AM7:00</td>", "<td>学生 PM7:00～AM7:00</td>"), "知らない文")
        self.assertRejected(page(FREE_SCHEDULE, "<td>会員様<br>AM0:00～AM5:00</td>"), "知らない文")
        self.assertRejected(page(FREE_SCHEDULE, "<td>AM0:00～AM5:00<br>日～木/祝日</td>"), "並び")

    def test_unknown_text_around_the_table_is_not_taken(self):
        self.assertRejected(page("☆ジュース飲み放題付き", "☆ジュース飲み放題付き（会員様料金）"), "説明が知らない文")
        self.assertRejected(page("※曜日・人数・時期により、料金が変動致します。",
                                 "※表示は会員料金です。曜日・人数・時期により、料金が変動致します。"), "注記が知らない文")
        self.assertRejected(between_tables("<p>※表示は税別です</p>"), "並び")
        self.assertRejected(between_tables("※表示は税別です"), "知らない文")
        self.assertRejected(page("DAY TIME</th>", "DAY TIME（平日）</th>"), "時間帯の見出し")

    def test_missing_or_duplicated_tables_are_not_taken(self):
        self.assertRejected(page('<h5 class="h5_tit01">料金表</h5>', "<h5>店内のご案内</h5>"), "料金表がありません")
        day, _, free = tables()
        self.assertRejected(page(day, free), "1 つずつ")
        self.assertRejected(page('<p class="mb-lg">', '<div class="mb-lg">').replace(
            "お問い合わせください。</p>", "お問い合わせください。</div>"), "並び")

    def test_shop_before_opening_is_not_taken(self):
        self.assertRejected(page('<a href="tel:0520000000">052-000-0000</a>',
                                 '<a href="tel:0520000000">052-000-0000　※オープン後より開通</a>'), "開店前")
        self.assertRejected(page("見本店でカラオケ・ダーツ！", "見本店が2027年夏GRAND OPEN予定！！"), "開店前")

    def test_notices_keep_their_period(self):
        record = u.parse_page(URL, with_notice("12/28～1/4は年末年始特別料金となります"), NOW)
        self.assertEqual(record["notices"], [
            {"text": "12/28~1/4は年末年始特別料金となります", "from": "2026-12-28", "until": "2027-01-04"}])
        record = u.parse_page(URL, with_notice("年末年始は特別料金となります。"), NOW)
        self.assertEqual(record["notices"][0]["until"], None)
        record = u.parse_page(URL, with_notice("2026年12月1日より料金改定"), NOW)
        self.assertEqual((record["notices"][0]["from"], record["notices"][0]["until"]), ("2026-12-01", None))
        issues = []
        v._joyjoy(record, None, "jj-Sample-Store", issues, require_checked_at=True)
        self.assertEqual(issues, [])

    def test_notices_outside_the_shop_section_are_ignored(self):
        # クーポン・スタッフ募集（年末年始手当、時給）・コメント・スクリプトは全店共通や料金と関係ない文
        record = u.parse_page(URL, PAGE, NOW)
        self.assertNotIn("notices", record)
        record = u.parse_page(URL, with_notice("新機種導入！ GW も元気に営業中"), NOW)
        self.assertNotIn("notices", record)


class NoticePeriodTests(unittest.TestCase):
    def period(self, text, seen=date(2026, 11, 26)):
        return u.notice_period(u.normalize(text), seen)

    def test_year_is_chosen_across_new_year(self):
        self.assertEqual(self.period("12/28～1/4は特別料金"), ("2026-12-28", "2027-01-04"))
        self.assertEqual(self.period("12/28～1/4は特別料金", date(2027, 1, 2)), ("2026-12-28", "2027-01-04"))
        self.assertEqual(self.period("1月1日～1月3日は特別料金"), ("2027-01-01", "2027-01-03"))
        self.assertEqual(self.period("11/1～11/3は特別料金"), ("2026-11-01", "2026-11-03"))  # 終わった告知
        self.assertEqual(self.period("2026年12月30日(水)～2027年1月3日(日)は年末年始料金"), ("2026-12-30", "2027-01-03"))
        self.assertEqual(self.period("12月30日(水)～1月3日(日)"), ("2026-12-30", "2027-01-03"))

    def test_open_ended_or_unreadable_periods(self):
        self.assertEqual(self.period("12月1日より料金改定"), ("2026-12-01", None))
        self.assertEqual(self.period("1/4まで年末年始料金"), (None, "2027-01-04"))
        self.assertEqual(self.period("～1/4は年末年始料金"), (None, "2027-01-04"))
        self.assertEqual(self.period("12/1(火) 料金改定のお知らせ"), ("2026-12-01", None))
        self.assertEqual(self.period("12/31は特別料金"), ("2026-12-31", "2026-12-31"))
        self.assertEqual(self.period("年末年始は特別料金"), (None, None))
        self.assertEqual(self.period("2/30～3/1は特別料金"), (None, None))
        self.assertEqual(self.period("12月下旬～1月上旬は特別料金"), (None, None))


class Response:
    def __init__(self, status=200, text="", url=URL):
        self.status_code, self.text, self.url = status, text, url
        self.encoding = "utf-8"
        self.content = text.encode("utf-8")


class Fetcher:
    def __init__(self, plan):
        self.plan, self.calls, self.bytes = plan, [], 0

    def get(self, url, *, html=False, **_):
        self.calls.append(url)
        outcome = self.plan[url]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def shop_url(name):
    return f"https://www.joyjoy.co.jp/store/view/{name}"


class UpdateTests(unittest.TestCase):
    NAMES = ["A", "B", "C", "D", "E", "F"]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / "joyjoy_prices.json"
        old = u.parse_page(URL, PAGE, NOW - 5 * 86400)
        self.previous = {f"jj-{name}": {**old, "page": shop_url(name)} for name in self.NAMES}
        self.data.write_text(json.dumps({"schema_version": 1, "updated_at": NOW - 5 * 86400,
                                         "shops": self.previous}, ensure_ascii=False), encoding="utf-8")
        chains = {f"jj-{name}": {"id": f"jj-{name}", "chain": "JOYJOY", "url": shop_url(name)} for name in self.NAMES}
        chains["be-1"] = {"id": "be-1", "chain": "ビッグエコー", "url": "https://shop.big-echo.jp/"}
        for patch in (mock.patch.object(u, "DATA_FILE", self.data),
                      mock.patch.object(u.update_chains, "load_previous", return_value=chains),
                      mock.patch.object(u.crawl_plan, "due", lambda ids, *args, **kwargs: list(ids))):
            patch.start()
            self.addCleanup(patch.stop)
        self.pauses = []

    def run_update(self, plan):
        fetcher = Fetcher({shop_url(name): outcome for name, outcome in plan.items()})
        logs = []
        result = u.update(log=logs.append, fetcher=fetcher, pause=self.pauses.append)
        saved = json.loads(self.data.read_text(encoding="utf-8"))
        return result, saved, logs, fetcher

    def test_network_failures_keep_previous_but_readable_pages_without_prices_do_not(self):
        result, saved, logs, _ = self.run_update({
            "A": Response(text=PAGE, url=shop_url("A")),
            "B": Response(text=page('<h5 class="h5_tit01">料金表</h5>', ""), url=shop_url("B")),
            "C": requests.ConnectTimeout("timed out"),
            "D": Response(status=404, text="not found", url=shop_url("D")),
            "E": Response(text=PAGE, url="https://www.joyjoy.co.jp/store/index/1"),
            "F": Response(text=page("30分</th>", "60分</th>"), url=shop_url("F")),
        })
        shops = saved["shops"]
        self.assertFalse(result)  # 通信の失敗が 1 店ある
        self.assertEqual(len(shops), 6)
        self.assertTrue(shops["jj-A"]["rates"])
        self.assertGreater(shops["jj-A"]["checked_at"], NOW)
        self.assertEqual(shops["jj-C"], self.previous["jj-C"])  # 通信の失敗だけ前回を残す（確認日時も進めない）
        for key, reason in (("jj-B", "料金表がありません"), ("jj-D", "HTTP 404"), ("jj-E", "転送"),
                            ("jj-F", "料金行の見出し")):
            self.assertEqual(shops[key]["rates"], [], key)
            self.assertIn(reason, shops[key]["withdrawn"], key)
            self.assertGreater(shops[key]["checked_at"], NOW, key)
            issues = []
            v._joyjoy(shops[key], self.previous[key], key, issues, require_checked_at=True)
            self.assertEqual(issues, [], key)
        self.assertEqual(saved["updated_at"], NOW - 5 * 86400)
        self.assertTrue(all(seconds >= 1 for seconds in self.pauses))
        self.assertEqual(len(self.pauses), 5)
        self.assertTrue(any("料金なし jj-B" in line for line in logs))

    def test_deadline_saves_what_was_fetched_and_ends_normally(self):
        result, saved, logs, fetcher = self.run_update({
            "A": Response(text=page("￥295～", "￥320～"), url=shop_url("A")),
            "B": net.OutOfTime("締め切り"),
            "C": Response(text=PAGE, url=shop_url("C")),
        })
        self.assertTrue(result)
        self.assertEqual(len(fetcher.calls), 2)
        self.assertEqual(saved["shops"]["jj-A"]["rates"][1]["windows"][0]["minimum_yen"], 320)
        for key in ("jj-B", "jj-C", "jj-D"):
            self.assertEqual(saved["shops"][key], self.previous[key])
        self.assertTrue(any("締め切り" in line for line in logs))

    def test_rate_limit_stops_the_rest(self):
        result, saved, _, fetcher = self.run_update({"A": Response(status=429, url=shop_url("A"))})
        self.assertFalse(result)
        self.assertEqual(len(fetcher.calls), 1)
        self.assertEqual(saved["shops"], self.previous)

    def test_server_errors_keep_previous(self):
        plan = {name: Response(status=500, url=shop_url(name)) for name in self.NAMES}
        result, saved, _, _ = self.run_update(plan)
        self.assertFalse(result)
        self.assertEqual(saved["shops"], self.previous)

    def test_every_shop_can_become_priceless(self):
        # 表の形が全店で変わっても、前回の最低価格に戻さず、全店を料金なしにする
        broken = page("30分</th>", "60分</th>", 2)
        result, saved, _, _ = self.run_update({name: Response(text=broken, url=shop_url(name)) for name in self.NAMES})
        self.assertTrue(result)
        self.assertTrue(all(shop["rates"] == [] for shop in saved["shops"].values()))


class DataCheckTests(unittest.TestCase):
    def check(self, record, *, source=True):
        issues = []
        v._joyjoy(record, None, "jj-Sample-Store", issues, require_checked_at=source)
        return issues

    def test_priceless_record_needs_a_reason(self):
        self.assertEqual(self.check(u.withdrawn(URL, "料金表がありません", NOW)), [])
        self.assertTrue(self.check({"page": URL, "rates": [], "checked_at": NOW}))
        self.assertTrue(self.check(u.withdrawn(URL, "料金表がありません", NOW), source=False))

    def test_amounts_and_known_texts_are_checked(self):
        def changed(edit):
            record = json.loads(json.dumps(u.parse_page(URL, PAGE, NOW)))
            edit(record)
            return self.check(record)

        self.assertTrue(changed(lambda r: r["rates"][0]["windows"][0].update(minimum_yen=79, price_text="¥79~")))
        self.assertEqual(changed(lambda r: r["rates"][0]["windows"][0].update(minimum_yen=80, price_text="¥80~")), [])
        self.assertTrue(changed(lambda r: r["rates"][2]["windows"][0].update(minimum_yen=200, price_text="¥200~")))
        self.assertTrue(changed(lambda r: r["rates"][0].update(charge="お一人様 60分")))
        self.assertTrue(changed(lambda r: r["rates"][1].update(included=["☆会員様はソフトドリンク飲み放題"])))
        self.assertTrue(changed(lambda r: r.update(note="※表示は会員料金です。")))
        self.assertTrue(changed(lambda r: r.update(withdrawn="料金表がありません")))
        self.assertTrue(changed(lambda r: r.update(notices=[{"text": "特別料金", "from": "12/28", "until": None}])))
        self.assertEqual(changed(lambda r: r.update(notices=[{"text": "特別料金", "from": None, "until": None}])), [])

    def test_sudden_change_from_previous_is_caught(self):
        old = u.parse_page(URL, PAGE, NOW - 3 * 86400)
        for night, flagged in ((295, False), (320, False), (140, True), (95, True), (600, True)):
            record = u.parse_page(URL, page("￥295～", f"￥{night}～"), NOW)
            issues = []
            v._joyjoy(record, old, "jj-Sample-Store", issues, require_checked_at=True)
            self.assertEqual(bool(issues), flagged, (night, issues))
        # 取り下げた記録は前回と比べない（前回の最低価格を残さない）
        issues = []
        v._joyjoy(u.withdrawn(URL, "料金表がありません", NOW), old, "jj-Sample-Store", issues, require_checked_at=True)
        self.assertEqual(issues, [])


if __name__ == "__main__":
    unittest.main()
