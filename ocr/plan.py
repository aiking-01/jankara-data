#!/usr/bin/env python3
"""OCR で読む画像を決めて、作業フォルダに置く。

対象は、カラオケ館の全店の 30 分料金の画像と、まねきねこで PDF の文字から金額を作れない店の 1 ページ目。
前回読んだものと同じ画像（まねきねこは PDF の etag、カラオケ館は画像の sha256 で判断）は読み直さない。

カラオケ館は、取れた画像の sha256 と取った時刻を kan_checks.json に書く。merge.py がこれを ocr.json の latest_key と
verified_at に移す（画像が読んだものと同じなら verified_at を進める）。scripts/web/ocr.js は、画像が変わったのに
まだ読めていない店と、14 日確かめられていない店を配らない。
- 通信の失敗（タイムアウト、接続エラー、本文の欠け、5xx など）の店は書かない。前回の記録が残り、14 日で配られなくなる
- 画像がもうない（404・410）か画像でない応答は、その日のうちに料金なしにする（読んだ画像と一致しない印を書く）
- 403・429・503 は断られているので、そのサイトの残りの画像は取らない（公式サイトに負担を掛けない）
- 確かめてから 2 日たっていない店は取らない（3 日に 1 回の定期実行の間に手で動かしても、取り直さない）
- CRAWL_DEADLINE を過ぎたら残りは取らず、取れた分で続ける

使い方: python ocr/plan.py <作業フォルダ>
作業フォルダに targets.json と kan_checks.json と画像を置く。
"""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image, UnidentifiedImageError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import net  # noqa: E402

OCR_FILE = ROOT / "source" / "ocr.json"
USER_AGENT = "jankara-price-navi/1.0 (personal price comparison)"
DELAY = 1.0
LONG_SIDE = 1800  # 縦長の料金表を、数字が潰れない大きさにそろえる
KAN_IMAGE = "https://karaokekan.net/shop2price/image/{number}/0"  # 30 分料金のタブはどの店も 0 番
# 文字の読めない PDF は確かめる手段が OCR しかないので、2 通りに読んで一致を確かめる
TWICE = {"no-text", "covered-number"}
# 画像がもうないとき。ocr.json の key（sha256）とは一致しないので、その店は配られなくなる
GONE = {404, 410}
# 断られているとき。残りの画像も取らない
REFUSED = {403, 429, 503}
# 確かめてからこの秒数がたっていない店は取らない（定期実行は 3 日に 1 回。開始時刻のずれを見込んで 2 日）
MIN_AGE = 2 * 86400


class FetchFailed(Exception):
    """通信の失敗。前回の記録を残す。"""


class Refused(Exception):
    """公式サイトに断られた。残りの画像は取らない。"""


def load_stored() -> dict:
    if not OCR_FILE.exists():
        return {}
    return json.loads(OCR_FILE.read_text(encoding="utf-8")).get("shops", {})


def fetch(fetcher: net.Fetcher, url: str, *, pdf: bool = False):
    """本文を返す。画像や PDF がもうなければ None。通信の失敗は FetchFailed。締め切りは net.OutOfTime。"""
    time.sleep(DELAY)
    try:
        response = fetcher.get(url, pdf=pdf)
    except net.OutOfTime:
        raise
    except Exception as error:  # タイムアウト、接続エラー、本文の欠け
        raise FetchFailed(f"{type(error).__name__}: {error}") from error
    if response.status_code in GONE:
        return None
    if response.status_code in REFUSED:
        raise Refused(f"HTTP {response.status_code}")
    if response.status_code != 200:
        raise FetchFailed(f"HTTP {response.status_code}")
    return response.content


def kan_image(data: bytes) -> Image.Image | None:
    """カラオケ館の画像を開く。画像でなければ None（料金を読めない）。途中で切れた画像は通信の失敗。"""
    if data.startswith(b"\xff\xd8") and b"\xff\xd9" not in data[-64:]:
        raise FetchFailed(f"画像が途中までしか届いていません ({len(data)} バイト)")
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except UnidentifiedImageError:
        return None
    except Exception as error:  # 壊れた画像
        raise FetchFailed(f"画像を開けません: {error}") from error
    return image.convert("RGB")


def render_pdf(data: bytes) -> Image.Image:
    import pypdfium2  # まねきねこの PDF だけで使う

    document = pypdfium2.PdfDocument(data)
    try:
        page = document[0]
        return page.render(scale=LONG_SIDE / max(page.get_size())).to_pil().convert("RGB")
    finally:
        document.close()


def maneki_targets() -> list[dict]:
    unread = json.loads(subprocess.run(["node", str(ROOT / "ocr" / "unread.mjs")], capture_output=True, check=True).stdout)
    shops = json.loads((ROOT / "source" / "maneki_prices.json").read_text(encoding="utf-8"))["shops"]
    targets = []
    for item in unread:
        shop = shops[item["id"]]
        if not shop.get("pdf"):
            continue
        targets.append({
            "id": item["id"],
            "chain": "まねきねこ",
            "url": shop["pdf"],
            "key": f"{shop['pdf']}#{shop.get('etag') or shop.get('modified') or ''}",
            "reads": 2 if item["reason"] in TWICE else 1,
        })
    return targets


def kan_targets() -> list[dict]:
    document = json.loads((ROOT / "chains.json").read_text(encoding="utf-8"))
    shops = document["shops"].values() if isinstance(document["shops"], dict) else document["shops"]
    return [{
        "id": shop["id"],
        "chain": "カラオケ館",
        "url": KAN_IMAGE.format(number=shop["id"].removeprefix("kan-")),
        "key": None,  # 画像を取得してから決める
        "reads": 2,
    } for shop in shops if shop.get("chain") == "カラオケ館"]


def recently_verified(record: dict | None, now: float) -> bool:
    """読んだ画像と同じだと MIN_AGE 以内に確かめた店か。画像が変わってまだ読めていない店は毎回取る"""
    if not record or record.get("latest_key") != record.get("key"):
        return False
    verified = record.get("verified_at")
    return isinstance(verified, int) and now - verified < MIN_AGE


def check_kan(fetcher: net.Fetcher, target: dict, stored: dict, checks: dict, work: Path) -> bool:
    """カラオケ館の 1 店の画像を取り、確かめた結果を checks に書く。読む対象なら画像を置いて True。"""
    data = fetch(fetcher, target["url"])
    image = kan_image(data) if data is not None else None
    if image is None:
        # 画像は取れたが料金がない。読んだ画像とは一致しない印を書き、その日のうちに料金なしにする
        checks[target["id"]] = {"key": "missing" if data is None else "not-image", "at": int(time.time())}
        print(f"::warning::{target['id']}: 料金の画像がありません ({'なし' if data is None else '画像でない応答'})", flush=True)
        return False
    target["key"] = hashlib.sha256(data).hexdigest()
    checks[target["id"]] = {"key": target["key"], "at": int(time.time())}
    if stored.get(target["id"], {}).get("key") == target["key"]:
        return False
    target["file"] = f"{target['id']}.jpg"
    image.save(work / target["file"], quality=92)
    return True


def main() -> None:
    work = Path(sys.argv[1])
    work.mkdir(parents=True, exist_ok=True)
    stored = load_stored()
    fetcher = net.Fetcher(USER_AGENT, label="ocr/plan")
    chosen, checks, failed, skipped = [], {}, 0, 0
    try:
        maneki = maneki_targets()
    except Exception as error:  # まねきねこの対象が決められなくても、カラオケ館は確かめる
        print(f"::warning::まねきねこの対象を決められません: {type(error).__name__}: {error}", flush=True)
        maneki = []
    now = time.time()
    refused = set()  # 断られたサイト。残りの画像は取らない
    try:
        for target in maneki + kan_targets():
            if target["key"] and stored.get(target["id"], {}).get("key") == target["key"]:
                continue
            if target["chain"] == "カラオケ館" and recently_verified(stored.get(target["id"]), now):
                skipped += 1
                continue
            host = urlsplit(target["url"]).hostname
            if host in refused:
                continue
            try:
                if target["chain"] == "カラオケ館":
                    if not check_kan(fetcher, target, stored, checks, work):
                        continue
                else:
                    data = fetch(fetcher, target["url"], pdf=True)
                    if data is None:
                        continue
                    target["file"] = f"{target['id']}.jpg"
                    render_pdf(data).save(work / target["file"], quality=92)
            except FetchFailed as error:  # 1 店の失敗で残りを止めない。前回の結果と記録は残る
                failed += 1
                print(f"::warning::{target['id']}: {error}", flush=True)
                continue
            except Refused as error:  # 前回の結果と記録は残る
                failed += 1
                refused.add(host)
                print(f"::warning::{target['id']}: {host} に断られたので ({error})、このサイトの残りは取りません", flush=True)
                continue
            except net.OutOfTime:
                raise
            except Exception as error:  # 画像の変換などの失敗。前回の結果を残す
                failed += 1
                print(f"::warning::{target['id']}: {type(error).__name__}: {error}", flush=True)
                continue
            chosen.append(target)
    except net.OutOfTime:
        print("::warning::締め切りを過ぎたので、残りの画像は取らずに、取れた分で続けます", flush=True)
    (work / "targets.json").write_text(json.dumps(chosen, ensure_ascii=False), encoding="utf-8")
    (work / "kan_checks.json").write_text(json.dumps(checks, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    reads = sum(target["reads"] for target in chosen)
    print(f"targets: {len(chosen)} images, {reads} reads, {failed} failed, {len(checks)} kan images checked, "
          f"{skipped} kan images checked recently, {fetcher.bytes:,} bytes", flush=True)


if __name__ == "__main__":
    main()
