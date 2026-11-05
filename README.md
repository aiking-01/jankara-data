# jankara-data

カラオケナビが使う店舗と料金のデータを公開するリポジトリです。
GitHub Actions が毎日 04:10（日本時間）に各チェーンの公式サイトから作ります。

## アプリへ配るもの（`dist/`）

| ファイル | 内容 |
|---|---|
| `manifest.json` | 各ファイルの sha256、大きさ、店の数、確認日時 |
| `chains.json.xz` | 10 チェーンの店舗（名前・住所・座標・公式の予約ページ） |
| `jankara.json.xz` | ジャンカラの料金 |
| `bigecho.json.xz` | ビッグエコーの金額（変換できた店だけ） |
| `maneki.json.xz` | まねきねこの金額（変換できた店だけ） |

通信量を抑えるため、アプリは毎日 `manifest.json` だけを読み、sha256 が変わったファイルだけをダウンロードします。
確認日時は `manifest.json` にあり、確認できなかった店にだけ店ごとの `checked_at` が残ります。

料金は参考情報です。正しい料金は各チェーンの公式サイトで確認してください。

## 作り方

- `chains.json` と `source/` は公式サイトから読んだ元データです。確認日時だけの変更はコミットしません。
- `scripts/convert.mjs` が料金表を金額へ変換し、読めない表は配りません。
- `scripts/publish.py` が xz で圧縮して `dist/` に置きます。店の数が 1 割以上減ったファイルは前回のものを残します。
- `scripts/web/bigecho.js` はアプリ本体と同じものです。変更するときは両方を直します。

## 安全な更新とデータの異常検査

`refresh.yml` と `ocr.yml` は共通の `scripts/push_data.sh` を経由し、更新前と圧縮前に `scripts/validate_data.py` で検査します。検査が 1 件でも失敗したら **push しません**。最新の `main` を比較元とし、更新内容が安全なことを確認できなければ `dist/` とデータを前回のまま維持します。

- 各チェーンの ID・店舗名・住所と都道府県・日本国内の座標・公式の店舗 URL を検査
- 店舗数（チェーンごと）と前回との差分、座標・店名・住所が突然別店舗になっていないかを検査
- ジャンカラの料金表：プラン／曜日／料金区分の列数・料金の異常値・大幅な価格変動・セル欠落を検査
- ビッグエコーの HTML 表、まねきねこの PDF 文字座標、カラオケ館等の OCR 出力を検査
- 配布データも前回公開した XZ と比較し、変換結果の異常な金額・大量欠落を検出

既存の未変更レコードは検査の基準に使い、更新・追加されたレコードを厳格に検査します。これにより、従来の不備が更新を妨げることを防ぎながら、新しい不正データは拒否します。

手動検査：`python scripts/validate_data.py --stage sources --baseline HEAD`（ソース更新後）／`python scripts/validate_data.py --stage full --baseline origin/main`（変換後）。実際の移転・料金改定で検出された場合は公式サイトを確認し、必要なら検証ロジックを調整してください。自動的な閾値緩和はしません。

## カラオケ館 B-30 型料金画像の自動OCR

`.github/workflows/kan-b30.yml` は毎日 **07:50 JST** と手動実行で、4曜日の料金画像を確認します。

1. `chains.json` に存在する `kan-...` の店舗IDと、公式サイトの4画像URLを `ocr/b30_targets.json` の `shops` に登録します。
2. `verified` には `ocr/b30_reviewed/kan-XXXX.json` のような目視確認済み料金JSONのパスを指定します。最初は `null` でも構いません。
3. Actions の **Read B-30 price images** を実行します。未確認または変更された画像は **kan-b30-review** 成果物内の `to_review.json` と元画像で全セルを確認し、必要なら修正して指定の `verified` パスへコミットします。
4. 再実行後、画像のSHA256と確認済みJSONが一致すれば `source/kan_b30.json` を更新し、従来の安全検査を通して `dist/kan.json.xz` に反映します。画像が更新されたときは自動承認せず、前回の料金を維持します。

設定例（画像URLは実際の4ファイルの直リンクに置き換えてください）：

```json
{
  "schema_version": 1,
  "shops": {
    "kan-1234": {
      "images": {
        "weekday": "https://karaokekan.net/actual-weekday-image.png",
        "friday": "https://karaokekan.net/actual-friday-image.png",
        "saturday": "https://karaokekan.net/actual-saturday-image.png",
        "sunday": "https://karaokekan.net/actual-sunday-image.png"
      },
      "verified": "ocr/b30_reviewed/kan-1234.json"
    }
  }
}
```

**注意:** 画像内のフリータイム欄から分かるのは「その金額が適用される入室時間帯」であり、必ずしも退室期限ではありません。終了時刻・最終受付が確認できない間は誤った見積もりを防ぐため、配布JSONに**30分料金だけ**を反映します。フリータイム料金はOCRの確認用成果物で保持します。また、三連休中日の独立した料金判定はアプリ側で未対応です。
