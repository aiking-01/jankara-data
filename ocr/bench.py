#!/usr/bin/env python3
"""料金表を OCR サーバーで読み、1 枚ごとの所要時間と結果を保存する。

使い方: python ocr/bench.py <出力フォルダ>
正解が分かる店（PDF に文字がある店、カラオケ館の見本）と、文字のない店を混ぜて読む。
"""
from __future__ import annotations

import base64
import io
import json
import sys
import time
from pathlib import Path

import pypdfium2
import requests
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SERVER = "http://127.0.0.1:11436/v1/chat/completions"
USER_AGENT = "jankara-price-navi/1.0 (personal price comparison)"
PROMPT = "OCR this image to HTML."
LONG_SIDE = 1800  # 縦長の料金表を、数字が潰れない大きさにそろえる

# (名前, まねきねこの店 ID または画像の URL, 種類)
TARGETS = [
    ("mn-toyama-ekimae-2nd-store", "text"),
    ("mn-chitose-ekimae-store", "text"),
    ("mn-nishikawaguchi-nishiguchi-store", "text"),
    ("mn-asaka-ekimae-store", "no-text"),
    ("mn-machida-1st-store", "no-text"),
    ("mn-kuki-store", "covered"),
    ("https://karaokekan.net/shop2price/image/35/0", "kan"),
]


def maneki_pdf(shop_id: str) -> str:
    shops = json.loads((ROOT / "source" / "maneki_prices.json").read_text(encoding="utf-8"))["shops"]
    return shops[shop_id]["pdf"]


def fetch(url: str) -> bytes:
    time.sleep(1)
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=60)
    response.raise_for_status()
    return response.content


def image_of(target: str, kind: str) -> Image.Image:
    if kind == "kan":
        return Image.open(io.BytesIO(fetch(target))).convert("RGB")
    document = pypdfium2.PdfDocument(fetch(maneki_pdf(target)))
    try:
        page = document[0]
        scale = LONG_SIDE / max(page.get_size())
        return page.render(scale=scale).to_pil().convert("RGB")
    finally:
        document.close()


def ocr(image: Image.Image) -> tuple[str, dict]:
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=92)
    body = {
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()}},
            {"type": "text", "text": PROMPT},
        ]}],
        "temperature": 0,
        "max_tokens": 4096,
    }
    response = requests.post(SERVER, json=body, timeout=3600)
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"], {"usage": data.get("usage"), "timings": data.get("timings")}


def main() -> None:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    summary = []
    for target, kind in TARGETS:
        name = target if kind != "kan" else "kan-35-0"
        try:
            image = image_of(target, kind)
            image.save(out / f"{name}.jpg", quality=85)
            started = time.time()
            text, stats = ocr(image)
            seconds = round(time.time() - started, 1)
            (out / f"{name}.html").write_text(text, encoding="utf-8")
            summary.append({"name": name, "kind": kind, "size": image.size, "seconds": seconds, **stats})
            print(name, kind, image.size, seconds, "s", flush=True)
        except Exception as error:  # 1 枚の失敗で残りを止めない
            summary.append({"name": name, "kind": kind, "error": str(error)})
            print(name, "error", error, flush=True)
        (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
