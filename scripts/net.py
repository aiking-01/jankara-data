"""公式サイトを読むときの共通の取得。チェーンごとの取得スクリプトから使う（ジャンカラは update_prices.py が別に持つ）。

- プロキシは途中で止まることがある（直接つなぐと 1 秒かからないページが 30〜60 秒で読み取りタイムアウトになる）。
  待ち時間を延ばしても効かないので、短めのタイムアウトで、毎回新しい接続から読み直す。
- プロキシの失敗が続いたら、そのスクリプトの残りは直接つなぐ（ジャンカラ以外は直接でも断られていない）。
- 途中までしか届いていない本文（</html> がない、Content-Length と長さが違う）は通信の失敗として扱う。
  後ろの行が欠けた料金表を、そのまま料金として読まないため。
- CRAWL_DEADLINE（UNIX 時刻）を過ぎたら OutOfTime を投げる。取得スクリプトはそこで打ち切り、取れた分を保存する。
  refresh.yml がジョブ全体の締め切りとして渡す。時間切れでジョブごと止まると、何も公開されないため。
"""
from __future__ import annotations

import os
import sys
import threading
import time

import requests

TIMEOUT = (10, 30)  # 接続、読み取りの間隔（全体の時間ではない）
ATTEMPTS = 3
DIRECT_AFTER = 3  # プロキシの失敗がこの回数続いたら直接つなぐ


class OutOfTime(Exception):
    """ジョブの締め切りを過ぎた。取得を打ち切って、取れた分を保存する。"""


class Incomplete(requests.ConnectionError):
    """本文が最後まで届いていない。通信の失敗と同じに扱う。"""


def deadline() -> float:
    try:
        return float(os.environ.get("CRAWL_DEADLINE") or 0)
    except ValueError:
        return 0.0


def out_of_time(margin: float = 0) -> bool:
    end = deadline()
    return bool(end) and time.time() + margin >= end


def check_deadline() -> None:
    if out_of_time():
        raise OutOfTime("締め切りを過ぎたので取得を打ち切ります")


def complete(response: requests.Response, *, html: bool = False, pdf: bool = False, json: bool = False) -> bool:
    """本文が最後まで届いたか。200 以外は確かめない（呼び出し側が状態で判断する）。"""
    if response.status_code != 200:
        return True
    body = response.content
    length = response.headers.get("Content-Length")
    if length and not response.headers.get("Content-Encoding"):
        try:
            if len(body) != int(length):
                return False
        except ValueError:
            pass
    if html and b"</html>" not in body[-4096:].lower():
        return False
    if pdf and b"%%EOF" not in body[-2048:]:
        return False
    if json and not body.rstrip().endswith((b"}", b"]")):
        return False
    return True


def _proxied() -> bool:
    return any(os.environ.get(name) for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"))


class Fetcher:
    """新しい接続で読み直し、プロキシが続けて失敗したら直接つなぐ取得。複数のスレッドから使ってよい。"""

    def __init__(self, user_agent: str, *, timeout=TIMEOUT, attempts: int = ATTEMPTS,
                 direct_after: int = DIRECT_AFTER, pause: float = 2.0, label: str = ""):
        self.user_agent = user_agent
        self.timeout = timeout
        self.attempts = attempts
        self.direct_after = direct_after
        self.pause = pause
        self.label = label
        self.direct = not _proxied()
        self.failures = 0
        self.bytes = 0
        self._lock = threading.Lock()

    def session(self) -> requests.Session:
        session = requests.Session()
        session.headers["User-Agent"] = self.user_agent
        if self.direct:
            session.trust_env = False  # HTTP(S)_PROXY を使わない
        return session

    def _failed(self, error: Exception) -> None:
        with self._lock:
            self.failures += 1
            if not self.direct and self.failures >= self.direct_after:
                self.direct = True
                print(f"{self.label}: プロキシの失敗が {self.failures} 回続いたので、残りは直接つなぎます ({type(error).__name__})",
                      file=sys.stderr, flush=True)

    def _succeeded(self, response: requests.Response) -> None:
        with self._lock:
            self.failures = 0
            self.bytes += len(response.content)

    def get(self, url: str, *, html: bool = False, pdf: bool = False, json: bool = False, headers: dict | None = None,
            allow_redirects: bool = True) -> requests.Response:
        """GET して応答を返す。通信の失敗が続けば最後の例外を投げる。HTTP の状態は確かめない。"""
        last: Exception | None = None
        for attempt in range(self.attempts):
            check_deadline()
            session = self.session()
            try:
                response = session.get(url, timeout=self.timeout, headers=headers, allow_redirects=allow_redirects)
                if not complete(response, html=html, pdf=pdf, json=json):
                    raise Incomplete(f"本文が途中までしか届いていません ({len(response.content)} バイト)")
                self._succeeded(response)
                return response
            except requests.exceptions.ProxyError as error:
                if "407" in str(error):
                    raise  # 認証の失敗は読み直しても通らない
                last = error
            except (requests.ConnectionError, requests.Timeout) as error:
                last = error
            finally:
                session.close()
            self._failed(last)
            if attempt + 1 < self.attempts:
                time.sleep(self.pause * (attempt + 1))
        assert last is not None
        raise last
