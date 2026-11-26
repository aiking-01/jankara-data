#!/usr/bin/env python3
"""アプリへ配るファイルを dist/ に作る。

アプリはまず manifest.json（数百バイト）だけを読み、sha256 が変わったファイルだけをダウンロードする。
確認日時はファイルの外（manifest.json）に置くので、料金が同じなら確認しただけではダウンロードが起きない。
"""
from __future__ import annotations

import hashlib
import json
import lzma
import os
import sys
import time
from pathlib import Path

import seen as seen_state

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
MANIFEST_VERSION = 2
SHOPS_VERSION = 1
SOURCES = {
    "joysound": ROOT / "build" / "joysound.json",
    "chains": ROOT / "chains.json",
    "jankara": ROOT / "source" / "prices.json",
    "bigecho": ROOT / "build" / "bigecho.json",
    "maneki": ROOT / "build" / "maneki.json",
    "kan": ROOT / "build" / "kan.json",
    "megabig": ROOT / "build" / "megabig.json",
    "round1": ROOT / "build" / "round1.json",
    "banban": ROOT / "source" / "banban_prices.json",
    "cotedazur": ROOT / "build" / "cotedazur.json",
    "joyjoy": ROOT / "build" / "joyjoy.json",
    "utahiro": ROOT / "build" / "utahiro.json",
}
# 前回よりこの割合を下回ったら、取得か変換の異常とみなして前回のファイルを残す。
# push_data.sh が元データの店数を検査した後（PUBLISH_VALIDATED=1）は使わない。変換が読まなくなった店で
# 減ったときに前回のファイルを残すと、期間限定の料金などで実際と違う金額を出し続けるため
MIN_RATIO = 0.9
# この日数より長く公式の料金表を確かめられていない店は配らない。取得が止まっても古い金額を出し続けない
STALE_DAYS = 14
# ジャンカラの配信ファイルは店の一覧も兼ねる。外すと店ごと消え、料金だけ外すと「この時間は料金なし」と出て
# 閉まっているように読めるので、アプリの表示を決めるまでは外さない（確認日時だけ進める）
KEEP_WHEN_STALE = {"jankara"}
# この回の巡回で確かめた店とみなす幅。確認できなかった店にだけ checked_at を残す
# 取得スクリプトが 20 時間以内の店を飛ばすので、それより長くする
RUN_SECONDS = 21 * 3600
COORD_DIGITS = 5  # 約 1m。距離の計算にはこれで足りる
# 辞書を小さくしておくと、スマホが展開するときのメモリも辞書の大きさで済む
XZ_FILTERS = [{"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME, "dict_size": 1 << 21,
               "lc": 4, "lp": 0, "pb": 0}]


def compact_shop(shop: dict, updated_at: int) -> dict:
    shop = dict(shop)
    if shop.get("checked_at", 0) >= updated_at - RUN_SECONDS:
        shop.pop("checked_at", None)
    for key in ("lat", "lng"):
        if isinstance(shop.get(key), float):
            shop[key] = round(shop[key], COORD_DIGITS)
    return shop


def compact(document: dict) -> dict:
    updated_at, shops = document["updated_at"], document["shops"]
    if isinstance(shops, dict):
        compacted = {key: compact_shop(shop, updated_at) for key, shop in shops.items()}
    else:
        compacted = [compact_shop(shop, updated_at) for shop in shops]
    return {"schema_version": SHOPS_VERSION, "shops": compacted}


def json_bytes(document: dict) -> bytes:
    # 配信される圧縮前の本文とSHA計算を必ず同じバイト列から作る。
    return json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def encode(document: dict) -> bytes:
    return lzma.compress(json_bytes(document), format=lzma.FORMAT_XZ,
                         check=lzma.CHECK_CRC32, filters=XZ_FILTERS)


def without_stale(name: str, document: dict, state: dict, now: int) -> tuple[dict, int]:
    """長く確かめられていない店を外した文書と、残った店を最後に確かめた日時を返す。"""
    source = seen_state.TRACKED.get(name)
    if source is None:
        return document, 0
    shops = document["shops"]
    items = shops.items() if isinstance(shops, dict) else ((str(item.get("id")), item) for item in shops)
    fresh, dropped, latest = {}, 0, 0
    for key, item in items:
        seen = seen_state.last_seen(state, source, key, item)
        if now - seen > STALE_DAYS * 86400 and name not in KEEP_WHEN_STALE:
            dropped += 1
            continue
        fresh[key] = item
        latest = max(latest, seen)
    if dropped:
        print(f"::warning::{name}: {STALE_DAYS} 日以上確かめられていない {dropped} 店の金額を配りません", flush=True)
    kept = fresh if isinstance(shops, dict) else [item for item in shops if str(item.get("id")) in fresh]
    return {**document, "shops": kept}, latest


def publish_file(name: str, document: dict, old: dict | None, dist: Path = DIST, *, confirmed_at: int = 0,
                 trust_count: bool = False) -> tuple[dict | None, bool]:
    """新しい manifest の項目と、公開できたかどうかを返す。配る店が 0 なら項目は None（manifest に載せない）。"""
    count = len(document["shops"])
    if count == 0 and (trust_count or not old):
        # アプリは manifest に count 0 の項目があると manifest 全体を読まない。そのチェーンだけ載せずに
        # ほかのチェーンを配る（アプリは manifest にないチェーンの金額を使わない）
        (dist / f"{name}.json.xz").unlink(missing_ok=True)
        print(f"{name}: 配れる店がないので manifest に載せません", flush=True)
        return None, True
    if old and not trust_count and count < old["count"] * MIN_RATIO:
        print(f"::error::{name}: 店の数が異常です ({old['count']} -> {count})。前回のファイルを残します", flush=True)
        return old, False
    compacted = compact(document)
    payload = json_bytes(compacted)
    # 圧縮とJSON-SHAに同一の本文バイト列を直接使う。
    data = lzma.compress(payload, format=lzma.FORMAT_XZ,
                         check=lzma.CHECK_CRC32, filters=XZ_FILTERS)
    (dist / f"{name}.json.xz").write_bytes(data)
    # 巡回済みでも元データが時刻だけの差分なら select_changes.py が巻き戻す。
    # 検証を通過した正常なRefreshだけ、manifest側の確認日時を進める。
    checked_at = max(document["updated_at"], old["checked_at"] if old else 0, confirmed_at)
    print(f"{name}: {count} 店 {len(data):,} バイト", flush=True)
    return {"sha256": hashlib.sha256(data).hexdigest(),
            "json_sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(data), "count": count, "checked_at": checked_at}, True


def main() -> int:
    # sourceの確認日時を時刻だけのコミットにせず、manifestだけ正常巡回を記録する。
    allowed = {"chains", "jankara", "bigecho", "maneki", "megabig", "joyjoy", "joysound", "cotedazur", "utahiro"}
    raw = os.environ.get("REFRESH_CONFIRMED_DATA", "")
    confirmed = {name for name in raw.split(",") if name}
    if not confirmed.issubset(allowed):
        print("::error::REFRESH_CONFIRMED_DATA contains unsupported source", flush=True)
        return 1
    confirmed_at = int(time.time())
    trust_count = os.environ.get("PUBLISH_VALIDATED") == "1"
    state = seen_state.load()
    DIST.mkdir(exist_ok=True)
    path = DIST / "manifest.json"
    old = json.loads(path.read_text(encoding="utf-8"))["files"] if path.exists() else {}
    if "jankara" not in old and "prices" in old:
        old["jankara"] = old.pop("prices")
    # 公開済みチェーンのソースが統合競合で消えても、manifestを欠損公開しない。
    unregistered = set(old) - set(SOURCES)  # 0 店で載せなかった名前は SOURCES にあるので数えない
    if unregistered:
        print(f"::error::既配信の料金ソースがpublishから消失: {sorted(unregistered)}", flush=True)
        return 1
    legacy_file, jankara_file = DIST / "prices.json.xz", DIST / "jankara.json.xz"
    if legacy_file.exists():
        if not jankara_file.exists():
            legacy_file.replace(jankara_file)
        else:
            legacy_file.unlink()
    files, published = {}, True
    for name, source in SOURCES.items():
        # ROUND1の登録済み・確認済み店舗がない間は空ファイルを公開しない。
        # 歌広場の変換結果は convert.mjs が必ず作るが、まだ一度も配っていなければ、ない場合も飛ばす
        if name in ("round1", "utahiro") and not source.exists() and name not in old:
            continue
        document = json.loads(source.read_text(encoding="utf-8"))
        document, latest_seen = without_stale(name, document, state, confirmed_at)
        # 確認日時は、料金が変わらなくても最後に公式の表を確かめた日時まで進める（アプリが古さの表示に使う）
        entry, ok = publish_file(name, document, old.get(name),
                                 confirmed_at=max(latest_seen, confirmed_at if name in confirmed else 0),
                                 trust_count=trust_count)
        if entry is not None:
            files[name] = entry
        elif name in ("chains", "jankara"):
            print(f"::error::{name}: 店がありません", flush=True)
            published = False
        published = published and ok
    manifest = {"schema_version": MANIFEST_VERSION, "files": files}
    path.write_text(json.dumps(manifest, separators=(",", ":"), sort_keys=True), encoding="utf-8", newline="")
    return 0 if published else 1


if __name__ == "__main__":
    sys.exit(main())
