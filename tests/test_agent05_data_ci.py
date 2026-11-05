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
import update


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

    def test_unpublished_optional_data_is_allowed(self):
        self.assertEqual(monitor.inspect(self.path, NOW), [])

    def test_all_published_optional_files_are_monitored(self):
        for name in monitor.OPTIONAL:
            path = self.path / "manifest.json"
            manifest = json.loads(path.read_text())
            payload = lzma.compress(b'{"schema_version":1,"shops":[{"id":1}]}')
            manifest["files"][name] = {
                "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload),
                "count": 1, "checked_at": NOW,
            }
            path.write_text(json.dumps(manifest))
            # manifestに登録された任意ファイルが無いときは必ず異常。
            self.assertTrue(any(name in err and "読めません" in err
                                for err in monitor.inspect(self.path, NOW)))
            (self.path / f"{name}.json.xz").write_bytes(payload)
            self.assertEqual(monitor.inspect(self.path, NOW), [])
            (self.path / f"{name}.json.xz").write_bytes(b"broken")
            self.assertTrue(any(name in err and "不一致" in err
                                for err in monitor.inspect(self.path, NOW)))
            (self.path / f"{name}.json.xz").write_bytes(payload)

    def test_new_daily_sources_are_checked_for_freshness(self):
        manifest_file = self.path / "manifest.json"
        document = json.loads(manifest_file.read_text())
        blob = lzma.compress(b'{"schema_version":1,"shops":[{"id":1}]}')
        for name in ("joyjoy", "joysound", "cotedazur", "banban", "round1"):
            document["files"][name] = {
                "sha256": hashlib.sha256(blob).hexdigest(),
                "size": len(blob), "count": 1, "checked_at": NOW - 100 * 3600,
            }
            (self.path / f"{name}.json.xz").write_bytes(blob)
        manifest_file.write_text(json.dumps(document))
        issues = monitor.inspect(self.path, NOW)
        for name in ("joyjoy", "joysound", "cotedazur"):
            self.assertTrue(any(name in issue and "最終正常確認" in issue for issue in issues))
        for name in ("banban", "round1"):
            self.assertFalse(any(name in issue and "最終正常確認" in issue for issue in issues))

    def test_unknown_manifest_filename_is_not_used_as_a_path(self):
        document = json.loads((self.path / "manifest.json").read_text())
        document["files"]["../../bad"] = document["files"]["kan"]
        (self.path / "manifest.json").write_text(json.dumps(document))
        self.assertTrue(any("未対応のmanifestファイル名" in issue
                            for issue in monitor.inspect(self.path, NOW)))

    def test_json_digest_matches_and_rejects_changed_payload(self):
        path = self.path / "manifest.json"
        document = json.loads(path.read_text())
        data = lzma.decompress((self.path / "chains.json.xz").read_bytes())
        document["files"]["chains"]["json_sha256"] = hashlib.sha256(data).hexdigest()
        path.write_text(json.dumps(document))
        self.assertEqual(monitor.inspect(self.path, NOW), [])
        document["files"]["chains"]["json_sha256"] = "0" * 64
        path.write_text(json.dumps(document))
        self.assertTrue(any("JSONハッシュ" in issue for issue in monitor.inspect(self.path, NOW)))

    def test_compressed_bomb_is_limited_before_json_hashing(self):
        path = self.path / "manifest.json"
        document = json.loads(path.read_text())
        raw = b"x" * 129
        packed = lzma.compress(raw)
        (self.path / "chains.json.xz").write_bytes(packed)
        document["files"]["chains"].update({
            "sha256": hashlib.sha256(packed).hexdigest(),
            "json_sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(packed)
        })
        path.write_text(json.dumps(document))
        with mock.patch.object(monitor, "MAX_JSON_BYTES", 128):
            issues = monitor.inspect(self.path, NOW)
        self.assertTrue(any("安全に展開できません" in issue for issue in issues), issues)

    def test_concatenated_xz_streams_are_not_accepted(self):
        path = self.path / "manifest.json"
        document = json.loads(path.read_text())
        raw = b'{"schema_version":1,"shops":[{"id":1}]}'
        packed = lzma.compress(raw) + lzma.compress(raw)
        (self.path / "chains.json.xz").write_bytes(packed)
        document["files"]["chains"].update({
            "sha256": hashlib.sha256(packed).hexdigest(),
            "json_sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(packed)
        })
        path.write_text(json.dumps(document))
        self.assertTrue(any("安全に展開できません" in issue
                            for issue in monitor.inspect(self.path, NOW)))

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

    def test_existing_published_chain_cannot_disappear_from_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            dist = Path(directory)
            original = {"schema_version": 2, "files": {
                "chains": {"sha256": "f" * 64, "size": 12, "count": 1, "checked_at": NOW},
                "joyjoy": {"sha256": "a" * 64, "size": 13, "count": 1, "checked_at": NOW},
            }}
            (dist / "manifest.json").write_text(json.dumps(original))
            with mock.patch.object(publish, "DIST", dist), \
                 mock.patch.object(publish, "SOURCES", {"chains": dist / "chains.json"}):
                self.assertEqual(publish.main(), 1)
            self.assertEqual(json.loads((dist / "manifest.json").read_text()), original)

    def test_unknown_confirmed_source_fails_closed(self):
        with mock.patch.dict(os.environ, {"REFRESH_CONFIRMED_DATA": "unknown"}):
            self.assertEqual(publish.main(), 1)


class LegacyShopRefreshTests(unittest.TestCase):
    def test_existing_shop_with_postal_mark_is_refetched(self):
        url = "https://karaokeclub.jp/shop/kuroiso/"
        old = {
            "url": url, "id": "dam-黒磯店", "reserve": "",
            "prefecture": "〒栃木県",
            "address": "〒栃木県那須塩原市鍋掛1086-202",
        }
        fixed = {**old, "prefecture": "栃木県",
                 "address": "栃木県那須塩原市鍋掛1086-202"}

        class Response:
            status_code = 200
            text = "verified-shop-html"

            def raise_for_status(self):
                pass

        with mock.patch.object(update, "request", return_value=Response()) as fetch, \
             mock.patch.object(update, "worker_session", return_value=object()), \
             mock.patch.object(update, "RequestPacer"):
            result = update.details([url], lambda *_: fixed, [old], object(), 0)
        self.assertEqual(result, [fixed])
        fetch.assert_called_once()

    def test_clean_existing_shop_is_still_skipped(self):
        url = "https://karaokeclub.jp/shop/kuroiso/"
        old = {
            "url": url, "id": "dam-黒磯店", "reserve": "",
            "prefecture": "栃木県",
            "address": "栃木県那須塩原市鍋掛1086-202",
        }
        with mock.patch.object(update, "request") as fetch, \
             mock.patch.object(update, "RequestPacer"):
            result = update.details([url], lambda *_: None, [old], object(), 0)
        self.assertEqual(result, [old])
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
