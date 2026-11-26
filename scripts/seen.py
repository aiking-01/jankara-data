#!/usr/bin/env python3
"""店ごとに、最後に公式の料金表を確かめた日時を source/seen.json に残す。

取得スクリプトは確かめた店の checked_at を進めるが、料金が変わらなければ select_changes.py が
元データを前回のものに戻すので、コミットされた checked_at からは「最近確かめたか」が分からない。
そこで select_changes.py の前にこのスクリプトで日時だけを別の小さなファイルに移す。
publish.py は、長く確かめられていない店の金額を配らない（人の確認なしに古い金額を引っ込める）。

tried には、取りに行ったのに確かめられなかった店（通信の失敗など）を {最後に試した日時, 続けて失敗した回数} で残す。
crawl_plan.py は、失敗が続く店を毎回は取りに行かない（公式サイトへの無駄なリクエストを減らす）。
lists には、店の一覧を最後に取り直せた日時を残す（週 1 回の取り直しを、曜日ではなく経過日数で決める）。

使い方:
  python scripts/seen.py mark                 確かめた日時を移し、失敗の記録を更新する
  python scripts/seen.py list-due chains 6    店の一覧を最後に取り直してから 6 日以上なら 0 で終わる
  python scripts/seen.py list-done chains     店の一覧を取り直せた日時を残す
"""
from __future__ import annotations

import json
import os
import sys
import time
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


def _ints(block) -> dict:
    return {str(k): v for k, v in block.items() if isinstance(v, int) and not isinstance(v, bool)}         if isinstance(block, dict) else {}


def load(path: Path | None = None) -> dict:
    path = path or SEEN_FILE
    empty = {"schema_version": SCHEMA_VERSION, "files": {}, "tried": {}, "lists": {}}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(doc, dict) or not isinstance(doc.get("files"), dict):
        return empty
    tried = {}
    for name, block in (doc.get("tried") or {}).items():
        if isinstance(block, dict):
            tried[name] = {str(k): [int(v[0]), int(v[1])] for k, v in block.items()
                           if isinstance(v, list) and len(v) == 2 and all(isinstance(x, int) for x in v)}
    return {"schema_version": SCHEMA_VERSION,
            "files": {name: _ints(block) for name, block in doc["files"].items() if isinstance(block, dict)},
            "tried": tried, "lists": _ints(doc.get("lists"))}


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
        state["files"][source] = block = block_for(doc, state["files"].get(source, {}))
        # 試した後に確かめられた店は失敗の記録を消す。確かめられなかった店は失敗の回数を 1 つ増やす
        tried = state["tried"].get(source, {})
        for key, (attempt, failures) in list(tried.items()):
            if key not in block or block[key] >= attempt:
                tried.pop(key)
            elif failures < 0:
                tried[key] = [attempt, -failures]  # 試しただけで、まだ結果を数えていない
        if tried:
            state["tried"][source] = tried
        else:
            state["tried"].pop(source, None)
    save(state, path)
    return state


def attempted(state: dict, source: str, keys, now: int) -> None:
    """crawl_plan.py が今回取りに行く店を記録する。結果は mark が数える（失敗なら回数を 1 増やす）"""
    block = state.setdefault("tried", {}).setdefault(source, {})
    for key in keys:
        failures = block.get(str(key), [0, 0])[1]
        block[str(key)] = [now, -(failures + 1)]  # 負の値は「数える前」


def last_seen(state: dict, source: str, key: str, item: dict) -> int:
    return max(state["files"].get(source, {}).get(key, 0), checked_at(item))


def main() -> int:
    args = sys.argv[1:]
    if args[:1] == ["list-due"] and len(args) == 3:
        last = load()["lists"].get(args[1], 0)
        return 0 if time.time() - last >= float(args[2]) * 86400 else 1
    if args[:1] == ["list-done"] and len(args) == 2:
        state = load()
        state["lists"][args[1]] = int(time.time())
        save(state)
        return 0
    if args != ["mark"]:
        print(__doc__, file=sys.stderr)
        return 2
    state = mark()
    for source, block in state["files"].items():
        print(f"seen: {source} {len(block)} 店", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
