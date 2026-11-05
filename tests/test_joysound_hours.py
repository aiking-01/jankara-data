"""公式の営業時間が曖昧な場合、LASTに推定閉店時刻を割り当てない。"""
import unittest

from bs4 import BeautifulSoup
from scripts.update_joysound_prices import official_simple_hours


class OfficialHoursTests(unittest.TestCase):
    def parse(self, html):
        return official_simple_hours(BeautifulSoup(html, "html.parser"))

    def test_plain_official_closing_time(self):
        self.assertEqual(self.parse("<dl><dt>営業時間</dt><dd>11:00～翌5:00</dd></dl>"),
                         "11:00～翌5:00")

    def test_special_weekday_closing_time_is_not_assumed(self):
        self.assertEqual(self.parse(
            "<dl><dt>営業時間</dt><dd>11:00～翌5:00<br>※日・祝は23:00まで</dd></dl>"
        ), "")

    def test_24_hours_is_not_closing_time(self):
        self.assertEqual(self.parse("<dl><dt>営業時間</dt><dd>24時間営業</dd></dl>"), "")


if __name__ == "__main__":
    unittest.main()
