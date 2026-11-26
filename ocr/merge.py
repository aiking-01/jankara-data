#!/usr/bin/env python3
"""並列のジョブが読んだ結果と、plan.py が確かめたカラオケ館の画像を source/ocr.json にまとめる。

使い方: python ocr/merge.py [--checks <kan_checks.json>] <結果ファイル>...
読めなかった店は前回の結果を残す。金額への変換は scripts/convert.mjs が行う。
カラオケ館は、最後に取れた画像の sha256 を latest_key に、それが読んだ画像（key）と同じだと確かめた時刻を
verified_at に書く。変換は、画像が変わったのにまだ読めていない店（latest_key が key と違う）と、
verified_at が古い店を配らない。読み取りの結果がない回（変わった画像がない回）も、この記録は積む。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OCR_FILE = ROOT / "source" / "ocr.json"
SCHEMA_VERSION = 1
# plan.py が書く今の画像の印。sha256 か、画像がないときの印
LATEST_KEY = re.compile(r"^(?:[0-9a-f]{64}|missing|not-image)$")


def load_checks(path: Path) -> dict:
    """plan.py の kan_checks.json を読む。形の違う記録は使わない（前回の記録が残る）"""
    if not path.exists():
        print(f"::warning::{path} がないので、画像を確かめた記録は進めません", flush=True)
        return {}
    checks = {}
    for key, check in json.loads(path.read_text(encoding="utf-8")).items():
        if (isinstance(check, dict) and isinstance(check.get("key"), str) and LATEST_KEY.match(check["key"])
                and isinstance(check.get("at"), int) and not isinstance(check["at"], bool) and check["at"] > 0):
            checks[key] = check
    return checks


def apply_checks(shops: dict, checks: dict) -> int:
    """確かめた画像の印を店に移す。読んだ画像と同じなら verified_at を進める。移した店の数を返す"""
    count = 0
    for key, check in checks.items():
        shop = shops.get(key)
        if not shop or shop.get("chain") != "カラオケ館":
            continue  # 読めた結果がまだない店は、変換の対象にならない
        shop["latest_key"] = check["key"]
        if check["key"] == shop.get("key"):
            verified = shop.get("verified_at")
            shop["verified_at"] = max(verified if isinstance(verified, int) else 0, check["at"])
        count += 1
    return count


def main() -> None:
    args = sys.argv[1:]
    checks = {}
    if args[:1] == ["--checks"]:
        checks = load_checks(Path(args[1]))
        args = args[2:]
    shops = json.loads(OCR_FILE.read_text(encoding="utf-8"))["shops"] if OCR_FILE.exists() else {}
    added = 0
    for name in args:
        for result in json.loads(Path(name).read_text(encoding="utf-8")):
            shops[result.pop("id")] = result
            added += 1
    # 読んだ結果で店を置き換えた後に移す（読んだ画像と今の画像が同じなら、その場で確かめたことになる）
    checked = apply_checks(shops, checks)
    payload = json.dumps({"schema_version": SCHEMA_VERSION, "updated_at": int(time.time()),
                          "shops": dict(sorted(shops.items()))}, ensure_ascii=False, separators=(",", ":"))
    temp = OCR_FILE.with_suffix(".json.tmp")
    temp.write_text(payload, encoding="utf-8", newline="")
    os.replace(temp, OCR_FILE)
    print(f"ocr: {added} updated, {checked} kan images checked, {len(shops)} stored", flush=True)


if __name__ == "__main__":
    main()
