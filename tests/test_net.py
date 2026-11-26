"""共通の取得（読み直し、直接接続への切り替え、本文の欠け、締め切り）を確かめる。通信はしない。"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import net  # noqa: E402


def response(body: bytes, status: int = 200, headers: dict | None = None) -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result._content = body
    result.headers.update(headers or {})
    return result


PAGE = b"<html><body>ok</body></html>"


class FetcherTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://proxy.invalid:1", "CRAWL_DEADLINE": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sleeps = mock.patch.object(net.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)

    def fetcher(self, results):
        fetcher = net.Fetcher("test", direct_after=2)
        sessions = []

        def make():
            session = mock.Mock()
            session.get.side_effect = lambda *args, **kwargs: _next(results)
            session.trust_env = True
            sessions.append(session)
            return session

        fetcher_session = fetcher.session

        def session():
            real = fetcher_session()
            fake = make()
            fake.trust_env = real.trust_env
            return fake

        fetcher.session = session
        return fetcher, sessions

    def test_reads_again_on_a_new_connection_after_a_timeout(self):
        fetcher, sessions = self.fetcher([requests.ReadTimeout("slow"), response(PAGE)])
        self.assertEqual(fetcher.get("https://example.invalid/", html=True).content, PAGE)
        self.assertEqual(len(sessions), 2)

    def test_switches_to_direct_after_repeated_proxy_failures(self):
        fetcher, sessions = self.fetcher([requests.ConnectionError("x"), requests.ConnectionError("y"), response(PAGE)])
        fetcher.get("https://example.invalid/", html=True)
        self.assertTrue(fetcher.direct)
        self.assertFalse(sessions[-1].trust_env)

    def test_cut_off_html_is_a_failure(self):
        fetcher, _ = self.fetcher([response(b"<html><table>")] * 3)
        with self.assertRaises(net.Incomplete):
            fetcher.get("https://example.invalid/", html=True)

    def test_length_mismatch_is_a_failure(self):
        self.assertFalse(net.complete(response(b"abc", headers={"Content-Length": "10"})))
        self.assertTrue(net.complete(response(b"abc", headers={"Content-Length": "3"})))
        # 圧縮された応答は展開後の長さが違うので比べない
        self.assertTrue(net.complete(response(b"abc", headers={"Content-Length": "2", "Content-Encoding": "gzip"})))
        self.assertFalse(net.complete(response(b"%PDF-1.4 ..."), pdf=True))
        self.assertFalse(net.complete(response(b'{"shops": [1, 2'), json=True))
        self.assertTrue(net.complete(response(b'{"shops": [1, 2]} \n'), json=True))

    def test_proxy_authentication_failure_is_not_retried(self):
        fetcher, sessions = self.fetcher([requests.exceptions.ProxyError("407 Proxy Authentication Required")])
        with self.assertRaises(requests.exceptions.ProxyError):
            fetcher.get("https://example.invalid/")
        self.assertEqual(len(sessions), 1)

    def test_stops_after_the_deadline(self):
        os.environ["CRAWL_DEADLINE"] = "1"
        fetcher, sessions = self.fetcher([response(PAGE)])
        with self.assertRaises(net.OutOfTime):
            fetcher.get("https://example.invalid/")
        self.assertEqual(sessions, [])


def _next(results):
    item = results.pop(0) if len(results) > 1 else results[0]
    if isinstance(item, Exception):
        raise item
    return item


if __name__ == "__main__":
    unittest.main()
