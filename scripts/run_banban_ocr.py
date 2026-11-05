#!/usr/bin/env python3
"""GitHub Actions用: 料金表画像4枚の安全な取り込みとOCRの起動。

画像は source/banban_images/<shop-id>/{weekday,weekend,solo_weekday,solo_weekend}.png
にコミットするか、workflow_dispatchで公開HTTPS URLを指定する。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
INPUT_DIR = ROOT / "source" / "banban_images"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
ALLOWED_HOSTS = {
    "github.com", "raw.githubusercontent.com", "user-images.githubusercontent.com",
    "private-user-images.githubusercontent.com", "karaoke-shin.jp", "www.karaoke-shin.jp",
}
ROLES = ("weekday", "weekend", "solo_weekday", "solo_weekend")


def allowed_url(value: str) -> bool:
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").lower()
    return (parsed.scheme == "https" and not parsed.username and not parsed.password
            and parsed.port in (None, 443)
            and (hostname in ALLOWED_HOSTS or hostname.endswith(".githubusercontent.com")))


def image_path(source: str, shop_id: str, role: str, stage: Path) -> Path:
    source = source.strip() or f"source/banban_images/{shop_id}/{role}.png"
    if "://" in source:
        if not allowed_url(source):
            raise ValueError(f"{role}: HTTPSのGitHub/公式画像URLのみ指定できます")
        target = stage / f"{role}.png"
        request = Request(source, headers={"User-Agent": "jankara-data-banban-ocr/1.0"})
        with urlopen(request, timeout=30) as response:
            if not allowed_url(response.url):
                raise ValueError(f"{role}: リダイレクト先が許可されていません")
            if int(response.headers.get("Content-Length", 0)) > MAX_IMAGE_BYTES:
                raise ValueError(f"{role}: 画像サイズが大きすぎます")
            data = response.read(MAX_IMAGE_BYTES + 1)
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise ValueError(f"{role}: 画像が空、または8MBを超えています")
        target.write_bytes(data)
    else:
        if Path(source).is_absolute():
            raise ValueError(f"{role}: リポジトリ内の相対パスを指定してください")
        target = (ROOT / source).resolve()
        if not target.is_relative_to(INPUT_DIR.resolve()):
            raise ValueError(f"{role}: source/banban_images/ 配下のファイルに限定しています")
        if not target.is_file():
            raise FileNotFoundError(f"{role}: 画像がありません: {source}")
        if target.stat().st_size > MAX_IMAGE_BYTES:
            raise ValueError(f"{role}: 画像が8MBを超えています")
    # HTMLのエラーページや偽装ファイルをOCRに渡さない。
    from PIL import Image
    with Image.open(target) as image:
        if image.format not in {"PNG", "JPEG", "WEBP"}:
            raise ValueError(f"{role}: PNG/JPEG/WebP画像ではありません")
        image.verify()
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shop-id", default="bb-00075")
    for role in ROLES:
        parser.add_argument("--" + role.replace("_", "-"), default="")
    args = parser.parse_args()
    if not args.shop_id.startswith("bb-") or len(args.shop_id) != 8 or not args.shop_id[3:].isdigit():
        parser.error("店舗IDはbb-00075の形式を指定してください")
    with tempfile.TemporaryDirectory(prefix="banban-ocr-") as temp:
        stage = Path(temp)
        inputs = [image_path(getattr(args, role), args.shop_id, role, stage) for role in ROLES]
        command = [sys.executable, str(ROOT / "scripts" / "extract_banban.py"),
                   "--shop-id", args.shop_id, "--output", str(ROOT / "source" / "banban_prices.json")]
        for role, path in zip(ROLES, inputs, strict=True):
            command += ["--" + role.replace("_", "-"), str(path)]
        subprocess.run(command, cwd=ROOT, check=True)
    print(f"{args.shop_id}: 画像を検証して料金JSONを生成しました")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"::error::BanBan画像抽出失敗: {error}", file=sys.stderr)
        sys.exit(1)
