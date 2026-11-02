"""実行: python -m unittest discover tests"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import update_prices as u  # noqa: E402


def shop(number=1, price=100, checked_at=1000, **extra):
    table = {"band": "昼", "customers": ["一般"], "note": "", "plans": [
        {"name": "30分", "hours": "8時～19時", "last_entry": "",
         "days": [{"day": "平日", "prices": {"一般": price}}]}]}
    return {"id": number, "name": "店", "lat": 34.0, "lng": 135.0, "alerts": [],
            "regular": [table], "solo": [], "checked_at": checked_at, **extra}


PAGE = """<html><body><h1 class="shopinfo__heading">テスト店</h1><div id="price">
<p>年末年始は特別料金です</p><style>.GW{}</style>
<table class="price_table"><tr class="price_name"><th class="price_title">昼</th><th class="attribute">一般</th></tr>
<tr><td class="price_time">30分<p>10時～19時</p></td><td class="wd">平日</td><td class="price">¥<span>100</span></td></tr></table>
</div></body></html>"""


class Alerts(unittest.TestCase):
    def test_notice_outside_table_is_detected(self):
        shop = u.parse_shop(1, PAGE)
        self.assertEqual(shop["alerts"], ["年末年始", "特別料金"])

    def test_words_in_style_are_ignored(self):
        self.assertNotIn("GW", u.parse_shop(1, PAGE)["alerts"])


class Merge(unittest.TestCase):
    def test_keeps_newer_single_shop_update(self):
        current = {1: shop(price=200, checked_at=2000)}
        merged = u.merge(current, {1: shop(price=100, checked_at=1000)}, [], 3000)
        self.assertEqual(merged[1]["checked_at"], 2000)

    def test_future_timestamp_does_not_block_updates(self):
        current = {1: shop(checked_at=999999)}
        merged = u.merge(current, {1: shop(checked_at=1000)}, [], 2000)
        self.assertEqual(merged[1]["checked_at"], 1000)

    def test_failed_shop_keeps_previous_value(self):
        merged = u.merge({1: shop(), 2: shop(2)}, {1: shop(checked_at=2000)}, [], 3000)
        self.assertIn(2, merged)
        self.assertEqual(merged[2].get("missing_strike", 0), 0)

    def test_closed_after_three_misses(self):
        data = {1: shop()}
        for expected in (False, False, True):
            data = u.merge(data, {}, [1], 3000)
            self.assertEqual(data[1]["closed"], expected)

    def test_returning_shop_is_reopened(self):
        new = shop(checked_at=2000)
        u.stamp(new, shop(missing_strike=2, closed=True), 2000)
        self.assertEqual(new["missing_strike"], 0)
        self.assertFalse(new["closed"])


class Validate(unittest.TestCase):
    def test_rejects_many_lost_shops(self):
        old = {i: shop(i) for i in range(1, 101)}
        with self.assertRaises(ValueError):
            u.validate({i: shop(i) for i in range(1, 90)}, old)

    def test_rejects_many_lost_prices(self):
        old = {i: shop(i) for i in range(1, 101)}
        new = {i: shop(i, price=None if i < 50 else 100) for i in range(1, 101)}
        with self.assertRaises(ValueError):
            u.validate(new, old)

    def test_accepts_small_change(self):
        old = {i: shop(i) for i in range(1, 101)}
        u.validate({i: shop(i) for i in range(1, 98)}, old)

    def test_rejects_non_shop_page(self):
        with self.assertRaises(u.PageError):
            u.parse_page(1, "<html><title>メンテナンス中</title></html>")


class Changed(unittest.TestCase):
    def test_price_change_is_detected(self):
        new = shop(price=150)
        self.assertTrue(u.stamp(new, shop(price=100), 5))
        self.assertEqual(new["changed_at"], 5)

    def test_alert_change_is_not_price_change(self):
        new = shop(alerts=["GW"])
        self.assertFalse(u.stamp(new, shop(changed_at=3), 5))
        self.assertEqual(new["changed_at"], 3)


if __name__ == "__main__":
    unittest.main()
