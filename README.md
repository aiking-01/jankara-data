# jankara-data

カラオケナビが使う店舗データ（6 チェーンの名前・住所・座標）を公開するリポジトリです。

GitHub Actions が毎日 04:10（日本時間）に各チェーンの公式サイトから `chains.json` を作ります。
料金は含みません。

https://raw.githubusercontent.com/aiking-01/jankara-data/main/chains.json

生成スクリプトは `scripts/update.py` です。手元で動かす場合:

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -p "test_*.py"
python scripts/update.py chains
```
