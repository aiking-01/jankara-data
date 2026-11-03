#!/usr/bin/env python3
"""作業フォルダの画像を OCR サーバーで読む。並列のジョブごとに担当を分ける。

使い方: python ocr/read.py <作業フォルダ> <担当番号> <ジョブ数> <出力ファイル>
2 回読む画像は、1 回目は元の大きさ、2 回目は縮小して読む。同じ画像を同じ設定で読むと同じ誤りを繰り返すため。
"""
from __future__ import annotations

import base64
import io
import json
import sys
import time
from pathlib import Path

import requests
from PIL import Image

SERVER = "http://127.0.0.1:11436/v1/chat/completions"
PROMPT = "OCR this image to HTML."
SCALES = (1.0, 0.85)
TIMEOUT = 1800


def ocr(image: Image.Image) -> str:
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
    response = requests.post(SERVER, json=body, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


def main() -> None:
    work, index, count, out = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), Path(sys.argv[4])
    targets = json.loads((work / "targets.json").read_text(encoding="utf-8"))
    mine = [target for number, target in enumerate(targets) if number % count == index]
    results = []
    for target in mine:
        image = Image.open(work / target["file"]).convert("RGB")
        reads = []
        started = time.time()
        try:
            for scale in SCALES[: target["reads"]]:
                sized = image if scale == 1.0 else image.resize((round(image.width * scale), round(image.height * scale)), Image.LANCZOS)
                reads.append(ocr(sized))
        except Exception as error:  # 読めなければ前回の結果を残す
            print(f"::warning::{target['id']}: {error}", flush=True)
            continue
        results.append({key: target[key] for key in ("id", "chain", "url", "key")} | {"reads": reads, "checked_at": int(time.time())})
        print(f"{target['id']}: {len(reads)} reads in {time.time() - started:.0f}s", flush=True)
        out.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
    out.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
