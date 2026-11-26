#!/usr/bin/env bash
# 元データを積み、最新の main の上で dist を作り直してプッシュする。
#
# 使い方: bash scripts/push_data.sh <コミットメッセージ> <元データのパス>...
#
# refresh.yml と ocr.yml は別々に動き、どちらも dist を書き換える。dist は圧縮ファイルなので、
# 後からプッシュする側は rebase でぶつかる。元データはワークフローごとに別のファイルなのでぶつからない。
# そこで元データだけを先に積み、プッシュに失敗したら dist を捨てて、相手の結果を含む最新の上で作り直す。
# 検査は --repair で動かす。異常のあった店は今回は配らず、店に結び付かない異常はそのチェーンだけ前回の
# 元データに戻して、残りのチェーンは公開する（1 件の異常で全部を止めると、人が直すまで何も更新されない）。
# 修復した後も検査に通らなければ、何もプッシュせずに失敗する。publish.py が失敗した場合も一切プッシュしない。
set -euo pipefail

message=$1
shift
ATTEMPTS=5

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

# Validate working tree before even creating a local source-data commit.
python scripts/validate_data.py --stage sources --baseline HEAD --repair

git add -- "$@"
[ -f source/seen.json ] && git add -- source/seen.json
git diff --cached --quiet || git commit -q -m "$message"

for attempt in $(seq 1 "$ATTEMPTS"); do
  git fetch -q origin main
  git rebase -q origin/main

  node scripts/convert.mjs
  node --test tests/*.test.mjs
  # Validate against latest main; scripts and OCR runs share the same gate.
  python scripts/validate_data.py --stage full --baseline origin/main --repair
  # 修復で元データが変わったら、その元データを積んでから変換と検査をやり直す
  if [ -n "$(git status --porcelain -- chains.json source)" ]; then
    git add -- chains.json source
    git commit -q -m "data: leave out shops that failed the data check"
    node scripts/convert.mjs
    python scripts/validate_data.py --stage full --baseline origin/main
  fi
  # 検査を通った元データの店数で公開する。変換が読まなくなった店（期間限定の料金など）で減っても前回を残さない
  # Publish must succeed completely: never push partial/old manifest data.
  PUBLISH_VALIDATED=1 python scripts/publish.py

  git add dist
  built=0
  if ! git diff --cached --quiet; then
    git commit -q -m "data: publish"
    built=1
  fi

  if git push -q origin HEAD:main; then
    echo "pushed on attempt $attempt"
    exit 0
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
