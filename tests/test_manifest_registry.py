"""複数PRを順次マージしたときに、既配信チェーンがpublishの対象から消えないことを検査。"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import publish


class PublishedRegistryTests(unittest.TestCase):
    def test_all_previously_published_names_are_preserved(self):
        manifest = json.loads((ROOT / "dist/manifest.json").read_text(encoding="utf-8"))
        published = set(manifest["files"])
        supported = set(publish.SOURCES)
        self.assertFalse(published - supported,
                         f"配信済みチェーンのpublishソースが失われました: {sorted(published - supported)}")
        self.assertTrue({"chains", "jankara", "bigecho", "maneki", "kan"} <= supported)

    def test_registered_sources_are_unique_and_have_safe_paths(self):
        for name, source in publish.SOURCES.items():
            with self.subTest(name=name):
                self.assertRegex(name, r"^[a-z][a-z0-9_]*$")
                self.assertTrue(source.is_relative_to(ROOT))
                self.assertEqual(source.suffix, ".json")


if __name__ == "__main__":
    unittest.main()
