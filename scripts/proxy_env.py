"""公式サイトを読む手順が使うプロキシの URL を組み立て、GITHUB_ENV の CRAWL_PROXY に書く。

Secrets の PROXY_USERNAME、PROXY_TOKEN、PROXY_ENDPOINT から
http://<ユーザー名>:<トークン>@<接続先>:23900 を作る。プロキシは接続ごとに出口の IP が変わる。

HTTPS_PROXY ではなく CRAWL_PROXY に書くのは、git push や GitHub API への通知まで
プロキシを通さないため。公式サイトを読む手順だけが env で HTTP(S)_PROXY に渡す。
Secrets がない（フォークからの PR など）ときは何も書かず、直接つなぐ。
空の HTTP(S)_PROXY は requests と urllib が無視するので、そのまま直接つながる。
"""
from __future__ import annotations

import os
import sys
from urllib.parse import quote

PORT = 23900


def proxy_url(env: dict[str, str]) -> str | None:
    user, token, host = (env.get(name, "").strip() for name in ("PROXY_USERNAME", "PROXY_TOKEN", "PROXY_ENDPOINT"))
    if not (user and token and host):
        return None
    return f"http://{quote(user, safe='')}:{quote(token, safe='')}@{host}:{PORT}"


def main() -> int:
    url = proxy_url(dict(os.environ))
    if url is None:
        print("::notice::プロキシの Secrets がないので、公式サイトへ直接つなぎます")
        return 0
    # トークンを URL エンコードした形もログに出さない
    print(f"::add-mask::{url}")
    token = quote(os.environ["PROXY_TOKEN"].strip(), safe="")
    print(f"::add-mask::{token}")
    with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as env_file:
        env_file.write(f"CRAWL_PROXY={url}\n")
    print("公式サイトを読む手順はプロキシを通します")
    return 0


if __name__ == "__main__":
    sys.exit(main())
