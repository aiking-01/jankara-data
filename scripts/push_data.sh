#!/usr/bin/env bash
# 元データを積み、最新の main の上で dist を作り直してプッシュする。
#
# 使い方: bash scripts/push_data.sh <コミットメッセージ> <元データのパス>...
#
# refresh.yml と ocr.yml は別々に動き、どちらも dist を書き換える。dist は圧縮ファイルなので、
# 後からプッシュする側は rebase でぶつかる。元データはワークフローごとに別のファイルなのでぶつからない。
# そこで元データだけを先に積み、プッシュに失敗したら dist を捨てて、相手の結果を含む最新の上で作り直す。
# 金額の確認に通らなければ、何もプッシュせずに失敗する。
# publish.py が前回のファイルを残した（店の数の急減）ときは、プッシュしたうえで失敗として終える。
set -euo pipefail

message=$1
shift
ATTEMPTS=5

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

git add -- "$@"
git diff --cached --quiet || git commit -q -m "$message"

for attempt in $(seq 1 "$ATTEMPTS"); do
  git fetch -q origin main
  git rebase -q origin/main

  node scripts/convert.mjs
  node --test tests/amounts.test.mjs tests/ocr.test.mjs
  published=0
  python scripts/publish.py || published=$?

  git add dist
  built=0
  if ! git diff --cached --quiet; then
    git commit -q -m "data: publish"
    built=1
  fi

  if git push -q origin HEAD:main; then
    echo "pushed on attempt $attempt"
    exit "$published"
  fi

  echo "::warning::main moved during the push (attempt $attempt); rebuilding dist on the latest data"
  if [ "$built" = 1 ]; then
    git reset -q --hard HEAD~1
  else
    git checkout -q -- dist
  fi
  sleep $((attempt * 15))
done

echo "::error::could not push after $ATTEMPTS attempts"
exit 1
