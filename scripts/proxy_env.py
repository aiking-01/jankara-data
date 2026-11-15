"""公式サイトを読む手順が使うプロキシの URL を組み立て、GITHUB_ENV の CRAWL_PROXY に書く。

Secrets の PROXY_USERNAME、PROXY_TOKEN、PROXY_ENDPOINT から
<方式>://<ユーザー名>:<トークン>@<接続先>:23900 を作る。プロキシは接続ごとに出口の IP が変わる。

方式（HTTP か SOCKS5）が分からないので、両方を実際に試して最初に通ったものを使う。
どちらも通らなければ直接つなぐ。通らないプロキシを渡すと、ジャンカラの巡回が
1 店ごとに再試行を重ね、Refresh 全体が時間切れになるため。

HTTPS_PROXY ではなく CRAWL_PROXY に書くのは、git push や GitHub API への通知まで
プロキシを通さないため。公式サイトを読む手順だけが env で HTTP(S)_PROXY に渡す。
Secrets がない（フォークからの PR など）ときは何も書かず、直接つなぐ。
空の HTTP(S)_PROXY は requests と urllib が無視するので、そのまま直接つながる。

--scheme と --require は診断用（proxy-probe.yml）。指定した方式だけを試し、通らなければ失敗で終える。
"""
from __future__ import annotations

import argparse
import os
import sys
from urllib.parse import quote

PORT = 23900
SCHEMES = ("http", "socks5h")
# 中身を見ない応答の速い URL。プロキシの先までつながるかだけを確かめる
CHECK_URL = "https://www.gstatic.com/generate_204"
CHECK_TIMEOUT = 15


def endpoint_host(value: str) -> str:
    """PROXY_ENDPOINT から接続先だけを取り出す。方式、認証、ポート、パスが付いていても外す。

    IPv6 は URL に入れるため [] で囲む。
    """
    host = value.strip()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0].rsplit("@", 1)[-1].strip()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    if host.count(":") == 1:
        host = host.split(":", 1)[0]
    elif host.count(":") > 1:
        host = f"[{host}]"
    return host


def proxy_url(env: dict[str, str], scheme: str = "http") -> str | None:
    user, token = (env.get(name, "").strip() for name in ("PROXY_USERNAME", "PROXY_TOKEN"))
    host = endpoint_host(env.get("PROXY_ENDPOINT", ""))
    if not (user and token and host):
        return None
    return f"{scheme}://{quote(user, safe='')}:{quote(token, safe='')}@{host}:{PORT}"


def works(url: str) -> tuple[bool, str]:
    """プロキシ越しに CHECK_URL へ届けば True。理由は URL を含まない短い文字列。"""
    import requests

    try:
        response = requests.get(CHECK_URL, proxies={"http": url, "https": url}, timeout=CHECK_TIMEOUT)
    except Exception as error:  # 認証失敗、方式違い、タイムアウトなど
        return False, type(error).__name__
    return response.status_code < 500, f"HTTP {response.status_code}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scheme", choices=SCHEMES, help="この方式だけを試す")
    parser.add_argument("--require", action="store_true", help="通らなければ失敗で終える")
    args = parser.parse_args(argv)

    env = dict(os.environ)
    if proxy_url(env) is None:
        print("::notice::プロキシの Secrets がないので、公式サイトへ直接つなぎます")
        return 1 if args.require else 0
    # トークンを URL エンコードした形もログに出さない
    print(f"::add-mask::{quote(env['PROXY_TOKEN'].strip(), safe='')}")

    for scheme in [args.scheme] if args.scheme else SCHEMES:
        url = proxy_url(env, scheme)
        print(f"::add-mask::{url}")
        ok, reason = works(url)
        print(f"プロキシ（{scheme}）: {'通った' if ok else '通らない'}（{reason}）")
        if ok:
            with open(env["GITHUB_ENV"], "a", encoding="utf-8") as env_file:
                env_file.write(f"CRAWL_PROXY={url}\n")
            print(f"公式サイトを読む手順は {scheme} のプロキシを通します")
            return 0

    print("::warning::プロキシにつながらないので、公式サイトへ直接つなぎます")
    return 1 if args.require else 0


if __name__ == "__main__":
    sys.exit(main())
