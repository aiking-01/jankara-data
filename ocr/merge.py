#!/usr/bin/env python3
"""並列のジョブが読んだ結果を source/ocr.json にまとめる。

使い方: python ocr/merge.py <結果ファイル>...
読めなかった店は前回の結果を残す。金額への変換は scripts/convert.mjs が行い、画像が変わった店の古い結果は使わない。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OCR_FILE = ROOT / "source" / "ocr.json"
SCHEMA_VERSION = 1


def main() -> None:
    shops = json.loads(OCR_FILE.read_text(encoding="utf-8"))["shops"] if OCR_FILE.exists() else {}
    added = 0
    for name in sys.argv[1:]:
        for result in json.loads(Path(name).read_text(encoding="utf-8")):
            shops[result.pop("id")] = result
            added += 1
    payload = json.dumps({"schema_version": SCHEMA_VERSION, "updated_at": int(time.time()),
                          "shops": dict(sorted(shops.items()))}, ensure_ascii=False, separators=(",", ":"))
    temp = OCR_FILE.with_suffix(".json.tmp")
    temp.write_text(payload, encoding="utf-8", newline="")
    os.replace(temp, OCR_FILE)
    print(f"ocr: {added} updated, {len(shops)} stored", flush=True)


if __name__ == "__main__":
    main()
