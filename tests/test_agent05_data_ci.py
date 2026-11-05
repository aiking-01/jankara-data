"""AI 5: data CI regression tests; entirely offline."""
from __future__ import annotations

import hashlib
import json
import lzma
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import monitor_freshness as monitor
import publish


NOW = 1_800_000_000


def sample_dist(path: Path, checked_at: int = NOW) -> None:
    files = {}
    for name in monitor.REQUIRED:
        data = lzma.compress(b'{"schema_version":1,"shops":[{"id":1}]}')
        (path / f"{name}.json.xz").write_bytes(data)
        files[name] = {
            "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
            "count": 1, "checked_at": checked_at,
        }
    (path / "manifest.json").write_text(
        json.dumps({"schema_version": 2, "files": files}), encoding="utf-8")


class ManifestMonitorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)
        sample_dist(self.path)

    def test_healthy_manifest_is_accepted(self):
        self.assertEqual(monitor.inspect(self.path, NOW), [])

    def test_refresh_staleness_is_detected(self):
        path = self.path / "manifest.json"
        data = json.loads(path.read_text())
        data["files"]["chains"]["checked_at"] = NOW - 73 * 3600
        path.write_text(json.dumps(data))
        issues = monitor.inspect(self.path, NOW)
        self.assertTrue(any("chains" in x and "最終正常確認" in x for x in issues), issues)

    def test_ocr_without_new_images_is_not_reported_stale(self):
        path = self.path / "manifest.json"
        data = json.loads(path.read_text())
        data["files"]["kan"]["checked_at"] = NOW - 100 * 86400
        path.write_text(json.dumps(data))
        self.assertEqual(monitor.inspect(self.path, NOW), [])

    def test_corrupted_xz_is_rejected(self):
        (self.path / "maneki.json.xz").write_bytes(b"broken")
        issues = monitor.inspect(self.path, NOW)
        self.assertTrue(any("maneki" in x and "不一致" in x for x in issues), issues)

    def test_missing_file_is_rejected(self):
        (self.path / "bigecho.json.xz").unlink()
        self.assertTrue(any("bigecho" in x and "読めません" in x
                            for x in monitor.inspect(self.path, NOW)))

    def test_future_checked_at_is_rejected(self):
        path = self.path / "manifest.json"
        data = json.loads(path.read_text())
        data["files"]["jankara"]["checked_at"] = NOW + 86400
        path.write_text(json.dumps(data))
        self.assertTrue(any("未来" in x for x in monitor.inspect(self.path, NOW)))


class ManifestConfirmationTests(unittest.TestCase):
    def test_validated_refresh_updates_only_manifest_clock_not_payload(self):
        document = {"updated_at": NOW - 86400 * 4, "shops": [
            {"id": "a", "checked_at": NOW - 86400 * 4, "lat": 35.123456}
        ]}
        with tempfile.TemporaryDirectory() as temp:
            dist = Path(temp)
            previous, ok = publish.publish_file("chains", document, None, dist)
            self.assertTrue(ok)
            data = (dist / "chains.json.xz").read_bytes()
            latest, ok = publish.publish_file("chains", document, previous, dist,
                                              confirmed_at=NOW)
            self.assertTrue(ok)
            self.assertEqual(latest["checked_at"], NOW)
            self.assertEqual(latest["sha256"], previous["sha256"])
            self.assertEqual((dist / "chains.json.xz").read_bytes(), data)

    def test_failed_refresh_must_not_advance_clock(self):
        document = {"updated_at": NOW - 86400, "shops": [{"id": "x"}]}
        with tempfile.TemporaryDirectory() as temp:
            dist = Path(temp)
            previous, _ = publish.publish_file("chains", document, None, dist)
            latest, _ = publish.publish_file("chains", document, previous, dist)
            self.assertEqual(latest["checked_at"], previous["checked_at"])

    def test_sharp_drop_still_refused_even_with_confirmation(self):
        document = {"updated_at": NOW, "shops": [{"id": "x"}]}
        old = {"sha256": "0" * 64, "size": 1, "count": 100, "checked_at": NOW - 86400}
        with tempfile.TemporaryDirectory() as temp:
            entry, ok = publish.publish_file("chains", document, old, Path(temp),
                                              confirmed_at=NOW)
            self.assertFalse(ok)
            self.assertIs(entry, old)

    def test_unknown_confirmed_source_fails_closed(self):
        with mock.patch.dict(os.environ, {"REFRESH_CONFIRMED_DATA": "unknown"}):
            self.assertEqual(publish.main(), 1)


if __name__ == "__main__":
    unittest.main()
