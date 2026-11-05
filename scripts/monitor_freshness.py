#!/usr/bin/env python3
"""配信manifestとXZの完全性・料金/店舗の最終正常巡回日時を確認する。

金額が不変でも正常Refreshでmanifest.checked_atを更新する。
OCRのkanは変更を検出したときだけ読み直すため鮮度閾値からは除外するが、
ハッシュ・サイズの完全性確認は全ファイルに行う。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

REQUIRED = ("chains", "jankara", "bigecho", "maneki", "kan")
REFRESHED = frozenset({"chains", "jankara", "bigecho", "maneki"})


def _utc(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")


def inspect(dist: Path, now: int, max_age_hours: float = 72) -> list[str]:
    issues: list[str] = []
    try:
        manifest = json.loads((dist / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"manifest.jsonが読めません: {exc}"]
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 2:
        return ["manifest.jsonのschema_versionが不正"]
    files = manifest.get("files")
    if not isinstance(files, dict):
        return ["manifest.jsonのfilesが不正"]
    for name in REQUIRED:
        entry = files.get(name)
        if not isinstance(entry, dict):
            issues.append(f"{name}: manifestの項目がありません")
            continue
        sha, size, count, checked_at = (
            entry.get("sha256"), entry.get("size"), entry.get("count"), entry.get("checked_at"))
        if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            issues.append(f"{name}: sha256が不正")
        if type(size) is not int or size <= 0 or type(count) is not int or count <= 0:
            issues.append(f"{name}: サイズ・店舗数が不正")
        if type(checked_at) is not int or checked_at <= 0 or checked_at > now + 3600:
            issues.append(f"{name}: checked_atが不正または未来")
        elif name in REFRESHED and now - checked_at > max_age_hours * 3600:
            issues.append(f"{name}: 最終正常確認が{(now - checked_at)/3600:.1f}時間前（上限{max_age_hours:g}時間）")
        if type(checked_at) is int and 0 < checked_at <= now + 3600:
            print(f"{name}: checked_at={_utc(checked_at)} / {count}店舗")
        path = dist / f"{name}.json.xz"
        try:
            data = path.read_bytes()
        except OSError as exc:
            issues.append(f"{name}: 配信XZが読めません: {exc}")
            continue
        if type(size) is int and len(data) != size:
            issues.append(f"{name}: XZサイズがmanifestと不一致")
        if isinstance(sha, str) and hashlib.sha256(data).hexdigest() != sha:
            issues.append(f"{name}: XZハッシュがmanifestと不一致")
        # 旧manifestには無い任意フィールド。存在する場合は配信本文と必ず照合する。
        json_sha = entry.get("json_sha256")
        if json_sha is not None:
            if not isinstance(json_sha, str) or len(json_sha) != 64 or any(
                    c not in "0123456789abcdef" for c in json_sha):
                issues.append(f"{name}: JSON SHA-256が不正")
            else:
                try:
                    import lzma
                    expanded = lzma.decompress(data)
                    if hashlib.sha256(expanded).hexdigest() != json_sha:
                        issues.append(f"{name}: JSONハッシュがmanifestと不一致")
                except (lzma.LZMAError, EOFError, ValueError):
                    issues.append(f"{name}: JSON検証時にXZを展開できません")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=Path(__file__).resolve().parents[1] / "dist")
    parser.add_argument("--max-hours", type=float, default=72)
    args = parser.parse_args()
    if not 0 < args.max_hours <= 24 * 30:
        parser.error("--max-hours must be between 0 and 720")
    issues = inspect(args.dist, int(time.time()), args.max_hours)
    if issues:
        for issue in issues:
            print(f"::error::{issue}", flush=True)
        return 1
    print("配信manifest/XZと正常確認日時は正常です", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
