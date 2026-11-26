"""公式サイトへのリクエストを抑えるため、今回取り直す店を決める。

料金はめったに変わらないので、店ごとに 3 日に 1 回、毎回は全体の 3 分の 1 ずつ、最後に確かめたのが古い順に取る。
最後に確かめた日時は source/seen.json から読む（料金が同じ日は select_changes.py が元データの checked_at を
前回のものに戻すので、元データだけを見ると毎日全店が古く見えてしまう）。
publish.py は 14 日確かめられない店を配らないので、7 日以上遅れている店は割り当てを超えても取る。
取りに行っても確かめられない店（通信の失敗が続く店）は、失敗の回数に応じて 1、2、4、7 日あけてから取り直す。
14 日以上確かめられていない店は publish.py がどうせ配らないので、割り当てを超えて急いでは取らない。
CRAWL_ALL=1 のときは全店を取る（手で動かすときや、取得の仕組みを変えた直後に使う）。
今回取る店は seen.json の tried に記録し、seen.py mark が結果を数える。
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
STALE = 14 * 86400
BACKOFF_DAYS = (1, 2, 4, 7)
SLACK = 4 * 3600  # 定期実行の開始時刻のずれ


def backing_off(state: dict, source: str, key: str, now: int) -> bool:
    attempt, failures = state.get("tried", {}).get(source, {}).get(key, (0, 0))
    if failures <= 0:
        return False
    wait = BACKOFF_DAYS[min(failures, len(BACKOFF_DAYS)) - 1] * 86400
    return now - attempt < wait - SLACK


def due(ids: Iterable, record: Callable[[str], dict | None], source: str, now: int | None = None, *,
        force: Callable[[str], bool] = lambda key: False, every_days: int = EVERY_DAYS,
        state: dict | None = None) -> list:
    """今回取り直す店の ID を、取る順に返す。ids の要素の型（int / str）はそのまま返す。"""
    ids = list(ids)
    now = int(time.time()) if now is None else now
    loaded = state is None
    state = seen.load() if loaded else state
    everything = os.environ.get("CRAWL_ALL") == "1"
    last = {key: seen.last_seen(state, source, str(key), record(key) or {}) for key in ids}
    waiting = set() if everything else {key for key in ids if backing_off(state, source, str(key), now)}
    # 新しい店と、取り直しが必要な店（表の範囲や URL が変わった店）は毎回取る（失敗が続く店は間をあける）
    first = [key for key in ids if (last[key] == 0 or force(key)) and key not in waiting]
    rest = sorted((key for key in ids if key not in first and key not in waiting and now - last[key] >= MIN_AGE),
                  key=lambda key: last[key])
    if everything:
        chosen = first + rest
    else:
        quota = math.ceil(len(ids) / max(1, every_days))
        late = sum(1 for key in rest if OVERDUE <= now - last[key] < STALE)
        chosen = first + rest[:max(quota - len(first), late, 0)]
    seen.attempted(state, source, chosen, now)
    if loaded and recording():
        seen.save(state)
    return chosen


def recording() -> bool:
    """試した記録を source/seen.json に残すのは Actions での実際の巡回だけ（テストや手元の実行では書かない）"""
    return os.environ.get("GITHUB_ACTIONS") == "true" and not os.environ.get("JANKARA_TESTING")
