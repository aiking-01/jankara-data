"""Offline parser and safety tests for the standalone data collector."""
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update.py"
spec = importlib.util.spec_from_file_location("karaoke_update", SCRIPT)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class ParsingTests(unittest.TestCase):
    def test_jankara_regular_price_and_location(self):
        html = """
        <h1 class="shopinfo__heading">ジャンカラ テスト店</h1>
        <script type="application/ld+json">{
          "address": {"addressRegion": "大阪府", "streetAddress": "大阪市中央区"},
          "geo": {"latitude": 34.66, "longitude": 135.50}
        }</script>
        <section id="price"><div class="price_table_area">
          <table class="price_table"><tr><th class="price_title">昼</th>
          <th class="attribute">会員</th><th class="attribute">一般</th></tr>
          <tr><td class="price_time">30分<p>10:00～18:00</p></td>
              <td class="wd">平日</td><td class="price">300円</td>
              <td class="price">400円</td></tr></table>
        </div>年末年始は特別料金</section>
        """
        parsed = module.parse_jankara(123, html)
        self.assertEqual(parsed["id"], 123)
        self.assertEqual(parsed["regular"][0]["plans"][0]["days"][0]["prices"]["会員"], 300)
        self.assertEqual(parsed["prefecture"], "大阪府")
        self.assertIn("年末年始", parsed["alerts"])

    def test_invalid_jankara_rejected(self):
        with self.assertRaises(ValueError):
            module.parse_jankara(2, "<html><title>Maintenance</title></html>")

    def test_big_echo_30_min_only(self):
        markup = """<dl><dt>営業時間</dt><dd>10:00-翌5:00</dd></dl>
          <div id="price"><h3>通常料金</h3><h4>30分料金</h4>
          <table><tr><th>区分</th><th>会員</th></tr>
          <tr><td>月〜金</td><td>180円</td></tr></table>
          <h4>フリータイム</h4><table>
          <tr><th>区分</th><th>会員</th></tr>
          <tr><td>月〜金</td><td>900円</td></tr></table></div>"""
        parsed = module.parse_be_price(markup)
        self.assertEqual(len(parsed["sections"]), 1)
        self.assertEqual(parsed["sections"][0]["rows"][0][1], "180円")
        self.assertIn("10:00", parsed["hours"])

    def test_address_and_big_echo_name_are_normalized(self):
        shop = module.shop_data("x", "x-1", "店", "〒100-0001 東京都千代田区１−２ センタービル", 35, 139, "https://example.test/")
        self.assertEqual("東京都千代田区1-2センタービル", shop["address"])

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"items": [{"key": "1", "name": "カラオケ ミューズ＆ビッグエコー 与次郎店", "address": "鹿児島県",
                                   "latitude": 31.5, "longitude": 130.5}]}

        with mock.patch.object(module, "request", return_value=Response()), mock.patch.object(module.time, "sleep"):
            self.assertEqual("ミューズ＆ビッグエコー 与次郎店", module.be_shops(None, 0)[0]["name"])

    def test_non_japanese_coordinate_rejected(self):
        self.assertIsNone(module.shop_data("ビッグエコー", "be-test", "Test", "", 0, 0, "https://example.test"))
        self.assertEqual(
            module.shop_data("ビッグエコー", "be-1", "店", "大阪府大阪市", 34.7, 135.5, "https://shop.big-echo.jp/map/1")["prefecture"],
            "大阪府",
        )

    def test_big_echo_url_host_block(self):
        class Fake:
            pass
        with self.assertRaises(ValueError):
            module.be_page_url(Fake(), "be-1", {"page": "https://malicious.example/"}, 0.5)


    def test_parallel_jankara_preserves_shop_order_and_stopping_rule(self):
        import tempfile
        from unittest.mock import patch

        class Response:
            def __init__(self, shop_id):
                self.status_code = 200 if shop_id in (1, 3) else 404
                self.text = "fixture"

            def raise_for_status(self):
                if self.status_code >= 400 and self.status_code != 404:
                    raise AssertionError("Unexpected status")

        def fake_request(session, url, redirect=False):
            number = int(url.rstrip("/").rsplit("/", 1)[1])
            return Response(number)

        def fake_parse(number, markup):
            return {
                "id": number, "name": f"shop-{number}",
                "regular": [{"plans": [{"days": [{"prices": {"会員": 100}}]}]}],
                "solo": [], "closed": False,
            }

        with tempfile.TemporaryDirectory() as location:
            with patch.object(module, "DATA", Path(location)), \
                 patch.object(module, "request", side_effect=fake_request), \
                 patch.object(module, "worker_session", return_value=object()), \
                 patch.object(module, "parse_jankara", side_effect=fake_parse), \
                 patch.object(module, "RequestPacer"):
                succeeded = module.update_jankara(start=1, end=70, stop_after=3, delay=0.125)

            self.assertTrue(succeeded)
            doc = module.read_json(Path(location) / "prices.json", {})
            self.assertEqual([item["id"] for item in doc["shops"]], [1, 3])
            self.assertTrue(doc["updated_at"])

    def test_skip_retired_sitemap_shop_but_keep_live_shops(self):
        from unittest.mock import patch

        class Page:
            def __init__(self, url):
                self.status_code = 404 if url.endswith("/56") else 200
                self.text = "valid html"

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise AssertionError("404 should be handled before raise_for_status")

        def parse(url, html):
            return {"url": url, "id": url.rsplit("/", 1)[-1]}

        urls = ["https://karaokekan.jp/shop/detail/56"] + [
            f"https://karaokekan.jp/shop/detail/{n}" for n in range(57, 76)
        ]
        with patch.object(module, "request", side_effect=lambda session, url: Page(url)), \
             patch.object(module, "worker_session", return_value=object()), \
             patch.object(module, "RequestPacer"):
            result = module.details(urls, parse, [], object(), 0.2)
        self.assertEqual([s["id"] for s in result], [str(n) for n in range(57, 76)])

    def test_fail_if_most_sitemap_shops_are_unavailable(self):
        from unittest.mock import patch
        class Missing:
            status_code = 404
        urls = [f"https://karaokekan.jp/shop/detail/{n}" for n in range(15)]
        with patch.object(module, "request", return_value=Missing()), \
             patch.object(module, "worker_session", return_value=object()), \
             patch.object(module, "RequestPacer"):
            with self.assertRaisesRegex(ValueError, "Too many unavailable"):
                module.details(urls, lambda u, h: None, [], object(), 0.2)


    def test_new_joysound_shop_and_renovation(self):
        url = "https://shop.joysound.com/shop/joysound-matsuyamaokaido1/"
        markup = """<title>JOYSOUND松山大街道一番町店 | JOYSOUND</title>
        "addressLocality": "松山市大街道", "addressRegion": "愛媛県",
        "streetAddress": "2-4-6 K-10ビル (受付1F)",
        "latitude": "33.8402852", "longitude": "132.7699526" """
        shop = module.parse_joysound(url, markup)
        self.assertEqual(shop["id"], "js-joysound-matsuyamaokaido1")
        self.assertEqual(shop["lat"], 33.840285)
        self.assertIsNone(module.parse_joysound(url, markup.replace("一番町店", "一番町店 リニューアル")))

    def test_cotedazur_xml_directory(self):
        markup = """<root><entry>
        <name>仙台泉店</name><position lat="38.327092" lng="140.883607" />
        <address>〒981-3133&lt;br /&gt;宮城県仙台市泉区泉中央3-2-7</address>
        <page>/branch/detail/10504.html</page></entry></root>"""
        shops = module.parse_cotedazur(markup)
        self.assertEqual(shops[0]["id"], "cd-10504")
        self.assertEqual(shops[0]["address"], "宮城県仙台市泉区泉中央3-2-7")

    def test_round1_only_if_karaoke_facility_is_listed(self):
        url = "https://www.round1.co.jp/shop/tenpo/tochigi-utunomiya.html"
        markup = """<title>【ラウンドワン】ラウンドワンスタジアム 宇都宮店｜店舗紹介</title>
        〒321-0102<br />栃木県宇都宮市江曽島町2072-1 ll=36.540686,139.886893"""
        navigation = '<li><img alt="カラオケ"></li>'
        without = navigation + '<h2>店舗で遊べる施設</h2><p>ボウリング</p><p>お知らせ</p>'
        with_karaoke = navigation + '<h2>店舗で遊べる施設</h2><img alt="カラオケ"><p>お知らせ</p>'
        self.assertIsNone(module.parse_round1(url, markup, without))
        self.assertEqual(module.parse_round1(url, markup, with_karaoke)["id"], "r1-tochigi-utunomiya")

    def test_joysound_uses_lastmod_for_incremental_updates(self):
        from unittest.mock import patch

        url = "https://shop.joysound.com/shop/joysound-new/"
        known = [{"url": url, "id": "js-joysound-new", "modified": "2026-11-03"}]
        class Response:
            text = f"<loc>{url}</loc><lastmod>2026-11-03</lastmod>"
            def raise_for_status(self):
                pass

        with patch.object(module, "request", return_value=Response()) as fetch:
            result = module.joysound_shops(object(), known, 0.2)
        self.assertEqual(result, known)
        self.assertEqual(fetch.call_count, 1)

    def test_skipped_not_karaoke_facility_is_not_a_failure(self):
        from unittest.mock import patch

        class Response:
            status_code = 200
            text = "example"
            def raise_for_status(self):
                pass

        urls = [f"https://shop.joysound.com/shop/joysound-{n}/" for n in range(12)]
        with patch.object(module, "request", return_value=Response()):
            with patch.object(module, "worker_session", return_value=object()):
                with patch.object(module, "RequestPacer"):
                    shops = module.details(
                        urls,
                        lambda url, markup: None if url.endswith("-0/") else {"url": url},
                        [], object(), 0.2, allow_unlisted=True,
                    )
        self.assertEqual(len(shops), 11)


if __name__ == "__main__":
    unittest.main()
