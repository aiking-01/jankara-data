"""公式サイトへのリクエストを抑えるため、今回取り直す店を決める。

料金はめったに変わらないので、店ごとに 3 日に 1 回、毎回は全体の 3 分の 1 ずつ、最後に確かめたのが古い順に取る。
最後に確かめた日時は source/seen.json から読む（料金が同じ日は select_changes.py が元データの checked_at を
前回のものに戻すので、元データだけを見ると毎日全店が古く見えてしまう）。
publish.py は 14 日確かめられない店を配らないので、7 日以上遅れている店は割り当てを超えても取る。
CRAWL_ALL=1 のときは全店を取る（手で動かすときや、取得の仕組みを変えた直後に使う）。
"""
from __future__ import annotations

import math
import os
import time
from typing import Callable, Iterable

import seen

EVERY_DAYS = 3
MIN_AGE = 20 * 3600  # 中断した実行を続きから進めるとき、確かめたばかりの店は取らない
OVERDUE = 7 * 86400


def due(ids: Iterable, record: Callable[[str], dict | None], source: str, now: int | None = None, *,
        force: Callable[[str], bool] = lambda key: False, every_days: int = EVERY_DAYS,
        state: dict | None = None) -> list:
    """今回取り直す店の ID を、取る順に返す。ids の要素の型（int / str）はそのまま返す。"""
    ids = list(ids)
    now = int(time.time()) if now is None else now
    state = seen.load() if state is None else state
    last = {key: seen.last_seen(state, source, str(key), record(key) or {}) for key in ids}
    # 新しい店と、取り直しが必要な店（表の範囲や URL が変わった店）は毎回取る
    first = [key for key in ids if last[key] == 0 or force(key)]
    rest = sorted((key for key in ids if key not in first and now - last[key] >= MIN_AGE), key=lambda key: last[key])
    if os.environ.get("CRAWL_ALL") == "1":
        return first + rest
    quota = math.ceil(len(ids) / max(1, every_days))
    late = sum(1 for key in rest if now - last[key] >= OVERDUE)
    return first + rest[:max(quota - len(first), late, 0)]
