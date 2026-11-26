#!/usr/bin/env python3
"""店ごとに、最後に公式の料金表を確かめた日時を source/seen.json に残す。

取得スクリプトは確かめた店の checked_at を進めるが、料金が変わらなければ select_changes.py が
元データを前回のものに戻すので、コミットされた checked_at からは「最近確かめたか」が分からない。
そこで select_changes.py の前にこのスクリプトで日時だけを別の小さなファイルに移す。
publish.py は、長く確かめられていない店の金額を配らない（人の確認なしに古い金額を引っ込める）。

使い方: python scripts/seen.py mark
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEEN_FILE = ROOT / "source" / "seen.json"
SCHEMA_VERSION = 1
# 配信名と元データ。カラオケ館（OCR）、BanBan と ROUND1（目視の承認）は巡回の仕組みが別なので含めない
TRACKED = {
    "jankara": "source/prices.json",
    "bigecho": "source/chain_prices.json",
    "maneki": "source/maneki_prices.json",
    "megabig": "source/megabig_prices.json",
    "joysound": "source/joysound_prices.json",
    "cotedazur": "source/cotedazur_prices.json",
    "joyjoy": "source/joyjoy_prices.json",
}


def records(doc: dict) -> dict:
    shops = doc.get("shops")
    if isinstance(shops, dict):
        return {str(key): item for key, item in shops.items() if isinstance(item, dict)}
    return {str(item["id"]): item for item in shops or [] if isinstance(item, dict) and "id" in item}


def checked_at(item: dict) -> int:
    value = item.get("checked_at")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def load(path: Path | None = None) -> dict:
    path = path or SEEN_FILE
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema_version": SCHEMA_VERSION, "files": {}}
    files = doc.get("files") if isinstance(doc, dict) else None
    if not isinstance(files, dict):
        return {"schema_version": SCHEMA_VERSION, "files": {}}
    return {"schema_version": SCHEMA_VERSION,
            "files": {name: {str(k): v for k, v in block.items() if isinstance(v, int)}
                      for name, block in files.items() if isinstance(block, dict)}}


def save(state: dict, path: Path | None = None) -> None:
    path = path or SEEN_FILE
    payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    temp = path.with_suffix(".json.tmp")
    temp.write_text(payload, encoding="utf-8", newline="")
    os.replace(temp, path)


def block_for(doc: dict, previous: dict) -> dict:
    """元データの店ごとに、前回までの日時と今の checked_at の新しい方を残す。閉店した店は消える。"""
    return {key: max(previous.get(key, 0), checked_at(item)) for key, item in records(doc).items()}


def mark(root: Path = ROOT, path: Path | None = None) -> dict:
    state = load(path)
    for source in TRACKED.values():
        file = root / source
        if not file.exists():
            continue
        doc = json.loads(file.read_text(encoding="utf-8"))
        state["files"][source] = block_for(doc, state["files"].get(source, {}))
    save(state, path)
    return state


def last_seen(state: dict, source: str, key: str, item: dict) -> int:
    return max(state["files"].get(source, {}).get(key, 0), checked_at(item))


def main() -> int:
    if sys.argv[1:] != ["mark"]:
        print(__doc__, file=sys.stderr)
        return 2
    state = mark()
    for source, block in state["files"].items():
        print(f"seen: {source} {len(block)} 店", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
