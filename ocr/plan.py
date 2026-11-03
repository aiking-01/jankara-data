#!/usr/bin/env python3
"""OCR で読む画像を決めて、作業フォルダに置く。

対象は、カラオケ館の全店の 30 分料金の画像と、まねきねこで PDF の文字から金額を作れない店の 1 ページ目。
前回読んだものと同じ画像（まねきねこは PDF の etag、カラオケ館は画像の sha256 で判断）は読み直さない。

使い方: python ocr/plan.py <作業フォルダ>
作業フォルダに targets.json と画像を置く。
"""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import time
from pathlib import Path

import pypdfium2
import requests
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
OCR_FILE = ROOT / "source" / "ocr.json"
USER_AGENT = "jankara-price-navi/1.0 (personal price comparison)"
DELAY = 1.0
LONG_SIDE = 1800  # 縦長の料金表を、数字が潰れない大きさにそろえる
KAN_IMAGE = "https://karaokekan.net/shop2price/image/{number}/0"  # 30 分料金のタブはどの店も 0 番
# 文字の読めない PDF は確かめる手段が OCR しかないので、2 通りに読んで一致を確かめる
TWICE = {"no-text", "covered-number"}


def load_stored() -> dict:
    if not OCR_FILE.exists():
        return {}
    return json.loads(OCR_FILE.read_text(encoding="utf-8")).get("shops", {})


def fetch(session: requests.Session, url: str) -> bytes:
    time.sleep(DELAY)
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return response.content


def render_pdf(data: bytes) -> Image.Image:
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


def main() -> None:
    work = Path(sys.argv[1])
    work.mkdir(parents=True, exist_ok=True)
    stored = load_stored()
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    chosen, failed = [], 0
    for target in maneki_targets() + kan_targets():
        if target["key"] and stored.get(target["id"], {}).get("key") == target["key"]:
            continue
        try:
            data = fetch(session, target["url"])
            if target["chain"] == "カラオケ館":
                target["key"] = hashlib.sha256(data).hexdigest()
                if stored.get(target["id"], {}).get("key") == target["key"]:
                    continue
                image = Image.open(io.BytesIO(data)).convert("RGB")
            else:
                image = render_pdf(data)
        except Exception as error:  # 1 店の失敗で残りを止めない。前回の結果は残る
            failed += 1
            print(f"::warning::{target['id']}: {error}", flush=True)
            continue
        target["file"] = f"{target['id']}.jpg"
        image.save(work / target["file"], quality=92)
        chosen.append(target)
    (work / "targets.json").write_text(json.dumps(chosen, ensure_ascii=False), encoding="utf-8")
    reads = sum(target["reads"] for target in chosen)
    print(f"targets: {len(chosen)} images, {reads} reads, {failed} failed", flush=True)


if __name__ == "__main__":
    main()
