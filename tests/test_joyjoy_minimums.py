"""JOYJOY公式最低料金を確定額として扱わないための回帰テスト。

コミット済みの source/ は読まない（店の増減やデータの変化で毎朝の Refresh 全体が止まるため）。
tests/fixtures の見本だけを読む。
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from update_joyjoy_prices import minimum_price  # noqa: E402
import validate_data as v  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "joyjoy_prices.json"


class JoyjoyMinimumTests(unittest.TestCase):
    def test_only_explicit_from_price_is_accepted(self):
        for text in ("￥450～", "450円～", "450〜", "¥450~"):
            value, original = minimum_price(text)
            self.assertEqual(value, 450)
            self.assertTrue(original)
        for text in ("450円", "450", "450-500円", "お問い合わせ", "無料"):
            with self.assertRaises(ValueError, msg=text):
                minimum_price(text)

    def test_sample_rates_remain_minimums_not_fixed_amounts(self):
        shops = json.loads(FIXTURE.read_text(encoding="utf-8"))["shops"]
        priced = {shop_id: shop for shop_id, shop in shops.items() if shop["rates"]}
        self.assertTrue(priced)
        for shop_id, shop in priced.items():
            rates = shop["rates"]
            self.assertEqual({rate["period"] for rate in rates},
                             {"daytime", "nighttime", "freetime"}, shop_id)
            for rate in rates:
                self.assertIn(rate["unit"], ("per_person_30_minutes", "per_person_per_freetime"))
                self.assertNotIn("regular", rate)
                for window in rate["windows"]:
                    minimum, _ = minimum_price(window["price_text"])
                    self.assertEqual(minimum, window["minimum_yen"], shop_id)

    def test_sample_records_pass_the_data_check(self):
        shops = json.loads(FIXTURE.read_text(encoding="utf-8"))["shops"]
        for shop_id, shop in shops.items():
            issues = []
            v._joyjoy(shop, None, shop_id, issues, require_checked_at=True)
            self.assertEqual(issues, [], shop_id)


if __name__ == "__main__":
    unittest.main()
