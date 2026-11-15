import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import proxy_env

SECRETS = {"PROXY_USERNAME": "user", "PROXY_TOKEN": "token", "PROXY_ENDPOINT": "203.0.113.5"}


class ProxyEnvTest(unittest.TestCase):
    def run_main(self, env, works=None, argv=()):
        with tempfile.TemporaryDirectory() as tmp:
            github_env = Path(tmp) / "env"
            env = {**env, "GITHUB_ENV": str(github_env)}
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(proxy_env, "works", works or (lambda url: (True, "HTTP 204"))), \
                    redirect_stdout(StringIO()) as out:
                code = proxy_env.main(list(argv))
            written = github_env.read_text(encoding="utf-8") if github_env.exists() else None
        return code, written, out.getvalue()

    def test_builds_url_and_escapes_credentials(self):
        url = proxy_env.proxy_url({**SECRETS, "PROXY_TOKEN": "a@b:c/d"}, "socks5h")
        self.assertEqual(url, "socks5h://user:a%40b%3Ac%2Fd@203.0.113.5:23900")

    def test_missing_secret_means_direct_connection(self):
        for missing in SECRETS:
            self.assertIsNone(proxy_env.proxy_url({**SECRETS, missing: " "}))

    def test_writes_crawl_proxy_not_https_proxy(self):
        code, written, out = self.run_main(SECRETS)
        self.assertEqual(code, 0)
        self.assertEqual(written, "CRAWL_PROXY=http://user:token@203.0.113.5:23900\n")
        self.assertIn("::add-mask::http://user:token@203.0.113.5:23900", out)

    def test_falls_back_to_socks_when_http_fails(self):
        code, written, _ = self.run_main(SECRETS, lambda url: (url.startswith("socks5h://"), "x"))
        self.assertEqual(code, 0)
        self.assertEqual(written, "CRAWL_PROXY=socks5h://user:token@203.0.113.5:23900\n")

    def test_unreachable_proxy_means_direct_connection(self):
        code, written, out = self.run_main(SECRETS, lambda url: (False, "ProxyError"))
        self.assertEqual(code, 0)
        self.assertIsNone(written)
        self.assertIn("::warning::", out)

    def test_require_fails_for_the_given_scheme(self):
        tried = []
        code, written, _ = self.run_main(SECRETS, lambda url: (tried.append(url) or False, "x"),
                                         ["--scheme", "socks5h", "--require"])
        self.assertEqual(code, 1)
        self.assertIsNone(written)
        self.assertEqual(tried, ["socks5h://user:token@203.0.113.5:23900"])

    def test_no_secrets_writes_nothing(self):
        code, written, _ = self.run_main({})
        self.assertEqual(code, 0)
        self.assertIsNone(written)


if __name__ == "__main__":
    unittest.main()
