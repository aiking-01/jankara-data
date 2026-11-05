"""ROUND1 B-30変換の基礎テスト。画像がなくても曜日・区分の対応を検証する。"""
import unittest

try:
    from scripts.extract_round1_b30 import ReadError, to_tables
except ImportError:
    ReadError = None
    to_tables = None


@unittest.skipIf(to_tables is None, "OCR画像用ライブラリをインストールしていません")
class TestB30Extract(unittest.TestCase):
    @staticmethod
    def cell(kind, column, start, end, yen, morning=False):
        return {"kind": kind, "column": column, "start": start, "end": end,
                "yen": yen, "morning": morning}

    def test_day_and_customer_mapping(self):
        cells = [
            self.cell("weekday", "student_slot", "開店", "13:00", 50),
            self.cell("weekday", "general_slot", "開店", "13:00", 50),
            self.cell("friday", "student_slot", "開店", "13:00", 50),
            self.cell("saturday", "student_slot", "6:00", "9:00", 320),
            self.cell("sunday", "student_slot", "6:00", "9:00", 320),
        ]
        slots = to_tables(cells)[0]
        self.assertEqual(slots["band"], "30分")
        weekday = next(p for p in slots["plans"] if p["hours"] == "開店～13:00")
        price = {row["day"]: row["prices"] for row in weekday["days"]}
        self.assertEqual(price["月火水木"]["学生"], 50)
        self.assertEqual(price["月火水木"]["シニア"], 50)
        self.assertEqual(price["月火水木"]["一般"], 50)
        self.assertEqual(price["金祝前"]["学生会員"], 50)
        self.assertEqual(price["土"], {})
        self.assertEqual(price["日祝"], {})

    def test_combined_free_cell_preserves_entry_range(self):
        free = to_tables([self.cell("weekday", "student_free", "開店", "18:00", 480, True)])[1]
        self.assertEqual(free["plans"][0]["hours"], "開店～18:00")
        self.assertEqual(free["plans"][0]["name"], "朝割フリータイム")

    def test_conflicting_prices_are_rejected(self):
        a = self.cell("weekday", "student_slot", "13:00", "18:00", 150)
        b = self.cell("weekday", "student_slot", "13:00", "18:00", 999)
        with self.assertRaises(ReadError):
            to_tables([a, b])


if __name__ == "__main__":
    unittest.main()
