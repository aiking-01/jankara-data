import json
import lzma
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import publish


def document(count, updated_at=1000000):
    return {"updated_at": updated_at, "shops": [{"id": str(i), "checked_at": updated_at, "lat": 35.123456789} for i in range(count)]}


class CompactTest(unittest.TestCase):
    def test_current_shop_drops_checked_at_and_rounds_coordinates(self):
        shop = publish.compact_shop({"checked_at": 1000000, "lat": 35.123456789, "lng": 135.0}, 1000000)
        self.assertEqual(shop, {"lat": 35.12346, "lng": 135.0})

    def test_shop_left_unchecked_keeps_its_date(self):
        shop = publish.compact_shop({"checked_at": 1000}, 1000000)
        self.assertEqual(shop["checked_at"], 1000)

    def test_clock_alone_does_not_change_the_file(self):
        self.assertEqual(publish.encode(publish.compact(document(3, 1000000))),
                         publish.encode(publish.compact(document(3, 1086400))))

    def test_dict_shops_keep_their_shape(self):
        result = publish.compact({"updated_at": 5, "shops": {"a": {"checked_at": 5, "regular": []}}})
        self.assertEqual(result, {"schema_version": 1, "shops": {"a": {"regular": []}}})

    def test_encoded_file_is_xz_json_without_clock(self):
        decoded = json.loads(lzma.decompress(publish.encode(publish.compact(document(1)))))
        self.assertEqual(set(decoded), {"schema_version", "shops"})


class PublishFileTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dist = Path(temp.name)

    def test_writes_file_and_entry(self):
        entry, ok = publish.publish_file("jankara", document(10), None, self.dist)
        self.assertTrue(ok)
        self.assertEqual(entry["count"], 10)
        self.assertEqual(entry["size"], (self.dist / "jankara.json.xz").stat().st_size)
        data = (self.dist / "jankara.json.xz").read_bytes()
        self.assertEqual(entry["json_sha256"],
                         __import__("hashlib").sha256(lzma.decompress(data)).hexdigest())

    def test_sharp_drop_keeps_previous(self):
        old = {"sha256": "x", "size": 1, "count": 100, "checked_at": 5}
        entry, ok = publish.publish_file("jankara", document(89), old, self.dist)
        self.assertFalse(ok)
        self.assertIs(entry, old)
        self.assertFalse((self.dist / "jankara.json.xz").exists())

    def test_checked_at_never_goes_back(self):
        old = {"sha256": "x", "size": 1, "count": 3, "checked_at": 2000000}
        entry, _ = publish.publish_file("jankara", document(3, 1000000), old, self.dist)
        self.assertEqual(entry["checked_at"], 2000000)


if __name__ == "__main__":
    unittest.main()
