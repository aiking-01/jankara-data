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


class ProxyEnvTest(unittest.TestCase):
    def test_builds_url_and_escapes_credentials(self):
        url = proxy_env.proxy_url({"PROXY_USERNAME": "user", "PROXY_TOKEN": "a@b:c/d", "PROXY_ENDPOINT": "203.0.113.5"})
        self.assertEqual(url, "http://user:a%40b%3Ac%2Fd@203.0.113.5:23900")

    def test_missing_secret_means_direct_connection(self):
        for missing in ("PROXY_USERNAME", "PROXY_TOKEN", "PROXY_ENDPOINT"):
            env = {"PROXY_USERNAME": "user", "PROXY_TOKEN": "token", "PROXY_ENDPOINT": "203.0.113.5"}
            env[missing] = " "
            self.assertIsNone(proxy_env.proxy_url(env))

    def test_writes_crawl_proxy_not_https_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            github_env = Path(tmp) / "env"
            env = {"PROXY_USERNAME": "user", "PROXY_TOKEN": "token", "PROXY_ENDPOINT": "203.0.113.5", "GITHUB_ENV": str(github_env)}
            with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(StringIO()) as out:
                self.assertEqual(proxy_env.main(), 0)
            self.assertEqual(github_env.read_text(encoding="utf-8"), "CRAWL_PROXY=http://user:token@203.0.113.5:23900\n")
            self.assertIn("::add-mask::http://user:token@203.0.113.5:23900", out.getvalue())

    def test_no_secrets_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            github_env = Path(tmp) / "env"
            with mock.patch.dict(os.environ, {"GITHUB_ENV": str(github_env)}, clear=True), redirect_stdout(StringIO()):
                self.assertEqual(proxy_env.main(), 0)
            self.assertFalse(github_env.exists())


if __name__ == "__main__":
    unittest.main()
