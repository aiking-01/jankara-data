"""料金の取得スクリプトが使う店舗の一覧。chains.json は update.py が作る。"""
from __future__ import annotations

import json
from pathlib import Path

DATA_FILE = Path(__file__).resolve().parents[1] / "chains.json"
USER_AGENT = "jankara-price-navi/1.0 (personal price comparison)"


def load_previous() -> dict[str, dict]:
    if not DATA_FILE.exists():
        return {}
    return {shop["id"]: shop for shop in json.loads(DATA_FILE.read_text(encoding="utf-8"))["shops"]}
