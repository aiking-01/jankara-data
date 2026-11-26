"""JOYSOUND の料金の取得: 問い合わせのみ・期間告知・読めないページを料金なしにし、通信の失敗だけ前回を残す。

実行: python -m unittest discover -s tests -p "test_*.py"
読むのは tests/fixtures の公式ページの抜粋だけ（source/ のデータは読まない）。
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import crawl_plan  # noqa: E402
import net  # noqa: E402
import seen  # noqa: E402
import update_joysound_prices as u  # noqa: E402
import validate_data as v  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
JST = timezone(timedelta(hours=9))
CONTACT = (FIXTURES / "joysound_contact.html").read_text(encoding="utf-8")
KANAYAMA = (FIXTURES / "joysound_kanayama.html").read_text(encoding="utf-8")
SEASON_TAG = 'data-from="2026-11-21T06:00:00+09:00" data-until="2027-01-12T06:00:00+09:00"'


def at(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=JST).timestamp())


def page(name: str) -> str:
    return f"https://shop.joysound.com/shop/joysound-{name}/"


def without_season(html: str) -> str:
    """.jt-season の告知を消した金山店（表だけのページ）"""
    start = html.index('<div class="jt-season')
    end = html.index("</div>", start) + len("</div>")
    while html.count("<div", start, end) > html.count("</div>", start, end):
        end = html.index("</div>", end) + len("</div>")
    return html[:start] + html[end:]


def with_note(html: str, note: str) -> str:
    """昼の料金欄の ※ 注記に 1 行足す"""
    return html.replace('<div class="notes">', f'<div class="notes"><p>{note}</p>', 1)


class ParsePageTests(unittest.TestCase):
    def test_contact_only_page_has_no_prices(self):
        record = u.parse_page(CONTACT, page("akitsu"), at("2026-11-26T12:00"))
        self.assertEqual(record["status"], "contact")
        self.assertEqual(record["sections"], [])
        self.assertEqual(record["scope"], u.SCOPE)

    def test_contact_with_other_wording_is_unreadable(self):
        changed = CONTACT.replace("料金は店舗にお問い合わせください", "料金は店舗でご確認ください")
        with self.assertRaises(ValueError):
            u.parse_page(changed, page("akitsu"), at("2026-11-26T12:00"))

    def test_season_notice_hides_the_table_during_the_season(self):
        record = u.parse_page(KANAYAMA, page("kanayama"), at("2026-11-26T12:00"))
        self.assertEqual(record["status"], "season")
        self.assertEqual(record["sections"], [])
        self.assertEqual(record["season_until"], at("2027-01-12T06:00"))

    def test_season_starting_within_21_days_is_already_hidden(self):
        # 告知は 11/21 から。11/1 の 21 日先（11/22 まで）と重なる
        self.assertEqual(u.parse_page(KANAYAMA, page("kanayama"), at("2026-11-01T12:00"))["status"], "season")
        record = u.parse_page(KANAYAMA, page("kanayama"), at("2026-10-01T12:00"))
        self.assertEqual(record["status"], "priced")
        # まだ先の告知は記録に残し、変換する日に判定し直す
        self.assertEqual([(n["from"], n["until"]) for n in record["notices"]],
                         [(at("2026-11-21T06:00"), at("2027-01-12T06:00"))])

    def test_table_comes_back_after_the_season(self):
        # 終わりの日（1/12 の朝 6 時まで）は今日の範囲に入るので、まだ出さない
        self.assertEqual(u.parse_page(KANAYAMA, page("kanayama"), at("2027-01-12T07:00"))["status"], "season")
        record = u.parse_page(KANAYAMA, page("kanayama"), at("2027-01-13T07:00"))
        self.assertEqual(record["status"], "priced")
        self.assertEqual(len(record["sections"]), 4)
        self.assertEqual(record["notices"], [])
        self.assertIn("※全プランワンドリンクオーダー制となります。(ドリンクバー付きプランは除く)", record["notes"]["昼"])

    def test_unreadable_end_of_notice_hides_the_table(self):
        html = KANAYAMA.replace(SEASON_TAG, 'data-from="2026-11-21T06:00:00+09:00" data-until="未定"')
        record = u.parse_page(html, page("kanayama"), at("2027-06-01T12:00"))
        self.assertEqual(record["status"], "season")
        self.assertIsNone(record["season_until"])

    def test_season_words_in_the_notes_hide_the_table(self):
        html = with_note(without_season(KANAYAMA), "※12/29〜1/3は年末年始特別料金となります。")
        self.assertEqual(u.parse_page(without_season(KANAYAMA), page("kanayama"), at("2026-12-10T12:00"))["status"], "priced")
        record = u.parse_page(html, page("kanayama"), at("2026-12-10T12:00"))
        self.assertEqual(record["status"], "season")
        self.assertEqual(record["season_until"], at("2027-01-04T06:00"))
        # 期間の 21 日より前は表を読み、告知は先の期間として残す
        early = u.parse_page(html, page("kanayama"), at("2026-11-01T12:00"))
        self.assertEqual(early["status"], "priced")
        self.assertEqual(early["notices"][0]["from"], at("2026-12-29T00:00"))

    def test_period_across_the_new_year_gets_the_right_years(self):
        html = with_note(without_season(KANAYAMA), "※年末年始(12月27日(日)〜1月5日(火))は特別料金です。")
        record = u.parse_page(html, page("kanayama"), at("2027-01-03T12:00"))
        self.assertEqual(record["status"], "season")
        self.assertEqual((record["notices"][0]["from"], record["notices"][0]["until"]),
                         (at("2026-12-27T00:00"), at("2027-01-06T06:00")))
        # 期間が過ぎたら、次の年末の告知として残す
        later = u.parse_page(html, page("kanayama"), at("2027-02-01T12:00"))
        self.assertEqual(later["status"], "priced")
        self.assertEqual(later["notices"][0]["from"], at("2027-12-27T00:00"))

    def test_season_words_without_dates_hide_the_table(self):
        html = with_note(without_season(KANAYAMA), "※年末年始は特別料金となります。")
        record = u.parse_page(html, page("kanayama"), at("2027-06-01T12:00"))
        self.assertEqual(record["status"], "season")
        self.assertIsNone(record["season_until"])

    def test_price_block_without_table_or_contact_is_unreadable(self):
        start = KANAYAMA.index('<div aria-label="平日の料金"')
        broken = KANAYAMA[:start].replace('class="jt"', 'class="x"') + KANAYAMA[start:].replace('class="jt"', 'class="x"')
        with self.assertRaises(ValueError):
            u.parse_page(broken, page("kanayama"), at("2027-06-01T12:00"))

    def test_page_of_another_shop_is_unreadable_and_block_page_is_a_fetch_failure(self):
        with self.assertRaises(ValueError):
            u.parse_page(KANAYAMA, page("aobadai"), at("2027-06-01T12:00"))
        with self.assertRaises(u.FetchFailed):
            u.parse_page("<html><body>Access Denied</body></html>", page("kanayama"), at("2027-06-01T12:00"))


class Response:
    def __init__(self, text="", status=200, url=None):
        self.text, self.status_code, self.url, self.encoding = text, status, url, None


class FakeFetcher:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        reply = self.replies[url]
        if isinstance(reply, BaseException):
            raise reply
        return reply


def priced(name, checked_at=1):
    return {"page": page(name), "scope": u.SCOPE, "status": "priced", "checked_at": checked_at, "notices": [],
            "notes": {"昼": [], "夜": []}, "opening_hours": "",
            "sections": [{"period": "昼", "day": "平日",
                          "rows": [{"label": "30分 11:00〜18:00", "prices": {"会員": 200}, "drinks": {"会員": ""}}]}]}


class UpdateTests(unittest.TestCase):
    NAMES = ["akitsu", "kanayama", "timeout", "legacy", "broken", "gone", "redirect"]

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        (root / "chains.json").write_text(json.dumps({"shops": [
            {"id": f"js-joysound-{name}", "chain": "JOYSOUND", "url": page(name)} for name in self.NAMES
        ]}), encoding="utf-8")
        for target, attribute, value in [
            (u, "ROOT", root), (u, "DATA_FILE", root / "source" / "joysound_prices.json"), (u, "DELAY", 0),
            (seen, "SEEN_FILE", root / "source" / "seen.json"), (crawl_plan, "recording", lambda: False),
        ]:
            patcher = mock.patch.object(target, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_update(self, previous, replies, now):
        u.save(previous, 5)
        fetcher = FakeFetcher(replies)
        lines = []
        ok = u.update(log=lines.append, fetcher=fetcher, now=now)
        return ok, fetcher, json.loads(u.DATA_FILE.read_text(encoding="utf-8")), lines

    def test_states_after_one_run(self):
        legacy = {**priced("legacy"), "scope": "standard-rates-v1"}
        legacy.pop("status")
        previous = {f"js-joysound-{name}": priced(name) for name in ["akitsu", "kanayama", "timeout", "broken", "gone", "redirect"]}
        previous["js-joysound-legacy"] = legacy
        replies = {
            page("akitsu"): Response(CONTACT),
            page("kanayama"): Response(KANAYAMA),
            page("timeout"): u.requests.Timeout("read timed out"),
            page("legacy"): u.requests.ConnectionError("proxy"),
            page("broken"): Response(CONTACT.replace("joysound-akitsu", "joysound-broken").replace("お問い合わせください", "ご確認ください")),
            page("gone"): Response(status=404),
            page("redirect"): Response(CONTACT, url="https://shop.joysound.com/"),
        }
        with mock.patch.dict("os.environ", {"CRAWL_ALL": "1"}):
            ok, fetcher, doc, lines = self.run_update(previous, replies, at("2026-11-26T12:00"))
        shops = doc["shops"]
        self.assertFalse(ok)  # 通信の失敗があった
        self.assertEqual(shops["js-joysound-akitsu"]["status"], "contact")
        self.assertEqual(shops["js-joysound-kanayama"]["status"], "season")
        # 通信の失敗は前回の記録のまま（seen.json の日時が進まないので 14 日で配らなくなる）
        self.assertEqual(shops["js-joysound-timeout"], previous["js-joysound-timeout"])
        # 旧形式の表は、取り直せなくても料金として残さない
        self.assertEqual(shops["js-joysound-legacy"]["status"], "unverified")
        self.assertEqual(shops["js-joysound-legacy"]["sections"], [])
        # ページは取れたのに読めない・ない・転送された店は、前回の金額を残さず料金なし
        for name in ["broken", "gone", "redirect"]:
            self.assertEqual(shops[f"js-joysound-{name}"]["status"], "unparsed", name)
            self.assertEqual(shops[f"js-joysound-{name}"]["sections"], [], name)
        # 読めないページは読み直さない（1 店 1 回）
        self.assertEqual(sorted(fetcher.calls), sorted(page(name) for name in self.NAMES))
        self.assertEqual(doc["updated_at"], 5)
        self.assertTrue(any("料金表 1, 店舗問い合わせ 1, 期間告知中 1, 読めない 3, 未確認 1" in line for line in lines))

    def test_everything_without_prices_is_still_saved(self):
        previous = {f"js-joysound-{name}": priced(name) for name in self.NAMES}
        replies = {page(name): Response(CONTACT.replace("joysound-akitsu", f"joysound-{name}")) for name in self.NAMES}
        with mock.patch.dict("os.environ", {"CRAWL_ALL": "1"}):
            ok, _, doc, _ = self.run_update(previous, replies, at("2026-11-26T12:00"))
        self.assertTrue(ok)
        self.assertEqual({shop["status"] for shop in doc["shops"].values()}, {"contact"})
        self.assertEqual(doc["updated_at"], at("2026-11-26T12:00"))

    def test_deadline_keeps_what_was_fetched(self):
        previous = {f"js-joysound-{name}": priced(name) for name in self.NAMES}
        replies = {page(name): net.OutOfTime("締め切り") for name in self.NAMES}
        replies[page("akitsu")] = Response(CONTACT)
        order = [f"js-joysound-{name}" for name in self.NAMES]
        with mock.patch.object(crawl_plan, "due", return_value=order):
            ok, fetcher, doc, _ = self.run_update(previous, replies, at("2026-11-26T12:00"))
        self.assertTrue(ok)
        self.assertEqual(len(fetcher.calls), 2)
        self.assertEqual(doc["shops"]["js-joysound-akitsu"]["status"], "contact")
        self.assertEqual(doc["shops"]["js-joysound-kanayama"], previous["js-joysound-kanayama"])

    def test_old_and_finished_season_records_are_fetched_again(self):
        now = at("2027-01-13T12:00")
        self.assertTrue(u.needs_refresh({**priced("a"), "scope": "standard-rates-v1"}, page("a"), now))
        self.assertTrue(u.needs_refresh(u.no_price(page("a"), "unverified", 0), page("a"), now))
        season = {**u.no_price(page("a"), "season", 1), "season_until": at("2027-01-12T06:00")}
        self.assertTrue(u.needs_refresh(season, page("a"), now))
        self.assertFalse(u.needs_refresh({**season, "season_until": at("2027-02-01T06:00")}, page("a"), now))
        self.assertFalse(u.needs_refresh(priced("a"), page("a"), now))


class PacingTests(unittest.TestCase):
    def test_requests_are_spaced_out(self):
        self.assertGreaterEqual(u.DELAY, 1)


class ValidateTests(unittest.TestCase):
    def check(self, item):
        issues = []
        v._joysound_raw(item, "js-joysound-a", issues)
        return issues

    def test_states_without_prices_pass(self):
        for status in ("contact", "unparsed", "unverified"):
            self.assertEqual(self.check(u.no_price(page("a"), status, 1)), [], status)
        season = {**u.no_price(page("a"), "season", 1), "season_until": None,
                  "notices": [{"from": at("2026-11-21T06:00"), "until": None, "text": "年末年始特別料金"}]}
        self.assertEqual(self.check(season), [])
        self.assertEqual(self.check({**season, "season_until": at("2027-01-12T06:00")}), [])

    def test_state_without_prices_must_not_carry_a_table(self):
        record = {**priced("a"), "status": "season", "season_until": 1}
        self.assertTrue(any("料金なしの状態" in issue for issue in self.check(record)))
        self.assertTrue(self.check({**u.no_price(page("a"), "season", 1), "season_until": "1/12"}))

    def test_priced_record_passes_and_old_or_unknown_records_do_not(self):
        self.assertEqual(self.check(priced("a")), [])
        legacy = {**priced("a"), "scope": "standard-rates-v1"}
        legacy.pop("status")
        self.assertTrue(any("旧形式" in issue for issue in self.check(legacy)))
        self.assertTrue(self.check({**priced("a"), "status": "ok"}))
        without = priced("a")
        without.pop("notices")
        self.assertTrue(self.check(without))
        self.assertTrue(self.check({**priced("a"), "notes": ["※1ドリンク制"]}))

    def test_no_priced_shop_at_all_does_not_stop_the_publish(self):
        self.addCleanup(setattr, v, "REPAIR", False)
        v.REPAIR = True
        issues = []
        old = {"shops": {f"js-joysound-{i}": {"checked_at": 1} for i in range(61)}}
        v._validate_doc("joysound", {"updated_at": 1, "shops": {}}, old, issues, built=True)
        self.assertEqual(issues, [])


if __name__ == "__main__":
    unittest.main()
