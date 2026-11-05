"""Offline regression tests for fail-closed source and dist validation."""
from __future__ import annotations

import json
import lzma
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import validate_data as v  # noqa: E402


def chain_shop(**extra):
    record = {
        "id": "dam-黒磯店", "chain": "CLUB DAM", "name": "黒磯店",
        "prefecture": "栃木県", "address": "栃木県那須塩原市鍋掛1086-202",
        "lat": 36.965419, "lng": 140.067155,
        "url": "https://karaokeclub.jp/shop/kuroiso/",
    }
    return {**record, **extra}


def tariff(price=300, *, customers=None, rows=None):
    customers = customers if customers is not None else ["会員", "一般"]
    rows = rows if rows is not None else {"会員": price, "一般": price + 100}
    return [{
        "band": "昼", "customers": customers, "plans": [{
            "name": "30分", "hours": "10:00～18:00",
            "days": [{"day": "月～金", "prices": rows}],
        }],
    }]


def jankara(price=300, **extra):
    return {
        "id": 1, "name": "新宿東口店", "prefecture": "東京都",
        "address": "東京都新宿区新宿1-2-3", "lat": 35.69, "lng": 139.70,
        "url": "https://jankara.ne.jp/shop/1/",
        "regular": tariff(price), "solo": [],
        **extra,
    }


class IdentityTests(unittest.TestCase):
    def test_correcting_legacy_postal_mark_does_not_trigger_identity_alarm(self):
        old = chain_shop(prefecture="〒栃木県", address="〒栃木県那須塩原市鍋掛1086-202")
        fresh = chain_shop()
        issues = []
        v._location(fresh, fresh["id"], issues, chain=True)
        v._identity(old, fresh, fresh["id"], issues)
        self.assertEqual(issues, [])

    def test_legacy_row_is_not_rejected_until_it_changes(self):
        old = chain_shop(prefecture="〒栃木県", address="〒栃木県那須塩原市鍋掛1086-202")
        doc = {"shops": {"dam-黒磯店": old}}
        issues = []
        _, changes = v._validate_doc("chains", doc, doc, issues)
        self.assertEqual(changes, 0)
        self.assertEqual(issues, [])

    def test_new_broken_address_rejected(self):
        original = chain_shop()
        broken = chain_shop(address="〒栃木県那須塩原市鍋掛1086-202")
        issues = []
        v._validate_doc("chains", {"shops": {"dam-黒磯店": broken}}, {"shops": {"dam-黒磯店": original}}, issues)
        self.assertTrue(any("住所が不正" in issue for issue in issues))

    def test_unauthorized_domain_and_wrong_id(self):
        broken = chain_shop(id="mn-fake", url="https://evil.example/shop/")
        issues = []
        v._location(broken, "dam-黒磯店", issues, chain=True)
        self.assertTrue(any("ドメイン" in issue for issue in issues))

    def test_other_store_coordinates_rejected(self):
        issues = []
        v._identity(chain_shop(), chain_shop(lat=35.69, lng=139.70), "dam-黒磯店", issues)
        self.assertTrue(any("10km" in issue for issue in issues))

    def test_same_coordinates_but_unrelated_address_rejected(self):
        issues = []
        v._identity(chain_shop(), chain_shop(address="栃木県宇都宮市別の住所1-2-3"), "dam-黒磯店", issues)
        self.assertTrue(any("住所が大きく変化" in issue for issue in issues))


class PricesTests(unittest.TestCase):
    def test_header_count_mismatch_rejected(self):
        issues = []
        v._price_values(tariff(customers=["会員", "一般"], rows={"会員": 300}),
                        "one", issues)
        self.assertTrue(any("列数" in issue for issue in issues))

    def test_gigantic_slot_price_rejected(self):
        issues = []
        v._jankara(jankara(30000), jankara(300), "1", issues)
        self.assertTrue(any("不自然な料金" in issue for issue in issues))

    def test_price_spike_rejected_even_if_shop_count_unchanged(self):
        issues = []
        v._jankara(jankara(1400), jankara(200), "1", issues)
        self.assertTrue(any("急騰" in issue for issue in issues))

    def test_price_change_in_normal_range_is_accepted(self):
        issues = []
        v._jankara(jankara(350), jankara(300), "1", issues)
        self.assertFalse(issues, issues)

    def test_lost_table_rejected(self):
        bad = jankara()
        bad["regular"] = []
        issues = []
        v._jankara(bad, jankara(), "1", issues)
        self.assertTrue(issues)

    def test_duplicate_ids_rejected(self):
        doc = {"shops": [jankara(), jankara()]}
        with self.assertRaisesRegex(v.DataRejected, "重複"):
            v._records(doc, "prices")


class RawAndPublishedTests(unittest.TestCase):
    def test_big_echo_missing_sections(self):
        issues = []
        v._bigecho_raw({"sections": []}, {"sections": [{"header": ["a", "b"], "rows": []}]}, "be-1", issues)
        self.assertTrue(any("大量欠落" in issue for issue in issues))

    def test_maneki_pdf_empty_after_previous_success(self):
        issues = []
        v._maneki_raw({"tables": []}, {"tables": [[["30分", 1, 2, 3, 4]]]}, "mn-x", issues)
        self.assertTrue(any("突然消失" in issue for issue in issues))

    def test_ocr_output_without_table_rejected(self):
        issues = []
        v._ocr({"chain": "カラオケ館", "reads": ["Gateway timeout and this is not a price table"], "url": "https://karaokekan.net/x"},
               None, "kan-1", issues)
        self.assertTrue(any("料金表 HTML" in issue for issue in issues))

    def test_converted_big_echo_sparse_customer_columns_are_valid(self):
        # Big Echo's converter declares all customer types but intentionally
        # emits only applicable ones; empty rows prevent holiday mispricing.
        table = tariff(customers=["学生会員", "学生", "会員", "シニア", "一般"],
                       rows={"会員": 350, "一般": 450})
        table[0]["plans"][0]["days"].insert(0, {"day": "祝", "prices": {}})
        old_table = tariff(customers=["学生会員", "学生", "会員", "シニア", "一般"],
                           rows={"会員": 300, "一般": 400})
        old_table[0]["plans"][0]["days"].insert(0, {"day": "祝", "prices": {}})
        issues = []
        v._generated({"regular": table, "solo": []},
                     {"regular": old_table, "solo": []}, "be-0141", issues)
        self.assertEqual(issues, [])

    def test_converted_price_unknown_customer_still_rejected(self):
        table = tariff(customers=["会員", "一般"], rows={"他店舗の区分": 300})
        issues = []
        v._generated({"regular": table, "solo": []}, None, "be-0141", issues)
        self.assertTrue(any("定義されていない料金区分" in issue for issue in issues))

    def test_converted_plan_with_only_empty_days_is_rejected(self):
        table = tariff(customers=["会員", "一般"], rows={})
        issues = []
        v._generated({"regular": table, "solo": []}, None, "be-0141", issues)
        self.assertTrue(any("有効な料金がありません" in issue for issue in issues))

    def test_source_price_columns_remain_strict(self):
        issues = []
        v._price_values(tariff(customers=["会員", "一般"], rows={"会員": 300}), "raw", issues)
        self.assertTrue(any("列数" in issue for issue in issues))

    def test_generated_amounts_and_missing_cells(self):
        issues = []
        good = {"regular": tariff(250), "solo": []}
        bad = {"regular": tariff(12500), "solo": []}
        v._generated(bad, good, "be-1", issues)
        self.assertTrue(any("不自然な料金" in issue for issue in issues))

    def test_many_changed_shops_trigger_format_change_alarm(self):
        old = {"shops": {f"be-{i}": {"sections": [], "hours": "9:00"} for i in range(100)}}
        new = {"shops": {f"be-{i}": {"sections": [], "hours": "10:00" if i < 40 else "9:00"}
                          for i in range(100)}}
        issues = []
        v._validate_doc("chain_prices", new, old, issues)
        self.assertTrue(any("35%" in issue for issue in issues))

    def test_unchanged_clock_not_counted_as_change(self):
        old = {"shops": {"be-1": {"sections": [], "checked_at": 10}}}
        new = {"shops": {"be-1": {"sections": [], "checked_at": 20}}}
        issues = []
        _, changes = v._validate_doc("chain_prices", new, old, issues)
        self.assertEqual(changes, 0)

    def test_baseline_lookup_failure_does_not_silently_skip_comparison(self):
        with mock.patch.object(v.subprocess, "run", return_value=mock.Mock(returncode=1, stdout=b"")):
            with self.assertRaises(v.DataRejected):
                v._old_bytes("unknown-ref", "chains.json")


if __name__ == "__main__":
    unittest.main()
