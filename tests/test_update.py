"""Offline parser and safety tests for the standalone data collector."""
import importlib.util
import re
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
        known = [{"url": url, "id": "js-joysound-new", "modified": "2026-11-03", "reserve": ""}]
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

    def test_reserve_links_come_from_each_official_shop_page(self):
        pages = {
            "まねきねこ": ('<a href="https://reservation.karaokemanekineko.jp/?storeId=669">',
                       "https://reservation.karaokemanekineko.jp/?storeId=669"),
            "カラオケ館": ('<a href="https://liff.line.me/1654959827-EpyGvRJ0/book/oa/@968yujxz/?utm_source=owned_media">',
                      "https://liff.line.me/1654959827-EpyGvRJ0/book/oa/@968yujxz/"),
            "JOYSOUND": ('<a href="https://reserve.joysound.com/reserve/shop_search?prefecture_id=38&amp;location_cd=40">',
                         "https://reserve.joysound.com/reserve/shop_search?prefecture_id=38&location_cd=40"),
            "コート・ダジュール": ('<a href="https://www.hotpepper.jp/strJ000726746/yoyaku/hpds/">',
                           "https://www.hotpepper.jp/strJ000726746/yoyaku/hpds/"),
            "BanBan": ('<a href="https://res-reserve.com/restaurants/banban129743">',
                       "https://res-reserve.com/restaurants/banban129743"),
            "CLUB DAM": ('<a href="https://big-echo.tottokun.com/ownedmedia/ownedmedia_immediately_be/D1Dn8a3HkpBCe9P">',
                         "https://big-echo.tottokun.com/ownedmedia/ownedmedia_immediately_be/D1Dn8a3HkpBCe9P"),
        }
        for chain, (markup, expected) in pages.items():
            self.assertEqual(module.reserve_link(chain, markup), expected, chain)
            self.assertEqual(module.reserve_link(chain, "<a href=\"/reserve/\">"), "", chain)

    def test_utahiroba_reserve_link_is_made_absolute(self):
        url = "https://www.utahiro.com/shops/st_shinjuku/205"
        markup = """<title>新宿三丁目店｜カラオケルーム歌広場</title>
            <iframe src="https://maps?!2d139.70!3d35.69"></iframe>
            >住所</div></div><div><div>東京都新宿区新宿3-1-1</div>
            <a href="/reserve/?store_name=新宿三丁目店">このサイトで予約する</a>"""
        shop = module.parse_utahiroba(url, markup)
        self.assertEqual(shop["reserve"],
                         "https://www.utahiro.com/reserve/?store_name=%E6%96%B0%E5%AE%BF%E4%B8%89%E4%B8%81%E7%9B%AE%E5%BA%97")

    def test_big_echo_reserve_is_asked_only_for_shops_without_one(self):
        from unittest.mock import patch

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"WEB予約への遷移": "https://big-echo.tottokun.com/ownedmedia/ownedmedia_immediately_be/Yc9JoH3F8cMJEDR"}

        with patch.object(module, "request", return_value=Response()) as fetch:
            pacer = module.RequestPacer(0)
            self.assertEqual(module.be_reserve(object(), "0077", {"reserve": "kept"}, pacer), "kept")
            self.assertEqual(module.be_reserve(object(), "0077", {"id": "be-0077"}, pacer),
                             "https://big-echo.tottokun.com/ownedmedia/ownedmedia_immediately_be/Yc9JoH3F8cMJEDR")
        self.assertEqual(fetch.call_count, 1)

    def test_maneki_page_without_map_link_and_shop_in_preparation(self):
        url = "https://www.karaokemanekineko.jp/locations/yamaguchi/yanai/yanai-store/"
        markup = """<title>柳井店｜カラオケまねきねこ</title>
            "geo": {"@type": "GeoCoordinates", "latitude": 33.962268, "longitude": 132.108396}
            <dt>住所</dt>
            <dd>
                <span>〒742-0031</span><br>
                <span>山口県柳井市南町 4-2-18</span>
            </dd>
            <dt>営業時間</dt><dd><span class="storeDetail__businessHoursRow__time">
                10:00～翌5:00</span></dd>"""
        shop = module.parse_maneki(url, markup)
        self.assertEqual(shop["id"], "mn-yanai-store")
        self.assertEqual(shop["address"], "山口県柳井市南町4-2-18")
        self.assertEqual((shop["lat"], shop["lng"]), (33.962268, 132.108396))
        self.assertIsNone(module.parse_maneki(url, markup.replace("10:00～翌5:00", "準備中")))
        with self.assertRaises(ValueError):
            module.parse_maneki(url, markup.replace("latitude", "x"))

    def test_banban_page(self):
        url = "https://karaoke-shin.jp/shop-list/00123.html"
        markup = """<title> カラオケBanBan 八雲店 | 店舗</title>
            <iframe src="https://maps?center=42.25,140.26"></iframe>
            〒049-3100<br />北海道二海郡八雲町富士見町47-1<br>営業時間：<b>10:00～翌2:00</b>"""
        shop = module.parse_banban(url, markup)
        self.assertEqual(shop["id"], "bb-00123")
        self.assertEqual(shop["name"], "八雲店")
        self.assertEqual(shop["prefecture"], "北海道")
        self.assertEqual(shop["hours"], "10:00～翌2:00")
        self.assertIsNone(module.parse_banban(url, markup.replace("center=", "x=")))

    def test_joyjoy_prefecture_comes_from_address_or_postal_code(self):
        url = "https://www.joyjoy.co.jp/store/view/sample"
        page = """<h2 class="t">%s<span>x</span></h2>!2d136.9!3d35.1
            <th>ADDRESS <small>住所</small></th>
            <td>%s　<a href="#">MAP</a></td>"""
        shop = module.parse_joyjoy(url, page % ("名駅店", "〒467-0066 名古屋市瑞穂区洲山町1-1"))
        self.assertEqual(shop["prefecture"], "愛知県")
        self.assertTrue(shop["address"].startswith("愛知県名古屋市"))
        shop = module.parse_joyjoy(url, page % ("可児店", "岐阜県可児市川合2607-26"))
        self.assertEqual(shop["address"], "岐阜県可児市川合2607-26")
        self.assertIsNone(module.parse_joyjoy(url, page % ("不明店", "どこか1-1")))
        shop = module.parse_joyjoy(url, page % ("笹島店", "名古屋市中村区名駅4丁目27-7"))
        self.assertEqual(shop["address"], "愛知県名古屋市中村区名駅4丁目27-7")
        shop = module.parse_joyjoy(url, page % ("上田店", "386-0018 長野県上田市常田3丁目5番35"))
        self.assertEqual(shop["address"], "長野県上田市常田3丁目5番35")
        moved = "（新）〒430-0906 静岡県浜松市中央区住吉4-14-5　　（旧）〒430-0906 静岡県浜松市中区住吉4-14-5"
        shop = module.parse_joyjoy(url, page % ("住吉店", moved))
        self.assertEqual(shop["address"], "静岡県浜松市中央区住吉4-14-5")
        self.assertEqual(shop["reserve"], "")
        gadget = """<script data-hpg-hpds-jcode="{'J001248996': 'null'}" src="hpds_variable_pc.js"></script>"""
        shop = module.parse_joyjoy(url, page % ("笹島店", "名古屋市中村区名駅4丁目27-7") + gadget)
        self.assertEqual(shop["reserve"], "https://www.hotpepper.jp/strJ001248996/yoyaku/hpds/")

    def test_club_dam_reads_title_and_either_map_form(self):
        url = "https://karaokeclub.jp/shop/x/"
        page = """<title>カラオケＣＬＵＢ ＤＡＭ &nbsp; カラオケＣＬＵＢ ＤＡＭ宇部店【禁煙】</title>
            %s<dt>住所：</dt><dd>〒755-0042&nbsp;山口県宇部市松島町15-8</dd>"""
        for maps in ("?ll=33.95,131.24&z=1", "embed?pb=!1d66!2d131.24!3d33.95!3m2"):
            shop = module.parse_clubdam(url, page % maps)
            self.assertEqual(shop["name"], "宇部店")
            self.assertEqual((shop["lat"], shop["lng"]), (33.95, 131.24))
        self.assertIsNone(module.parse_clubdam(url, page % ""))

    def test_club_dam_postal_whitespace_is_removed(self):
        url = "https://karaokeclub.jp/shop/kuroiso/"
        postal_forms = (
            "〒 325-0013&nbsp;", "〒&nbsp;325-0013&nbsp;",
            "〒325-0013&nbsp;", "〒　３２５－００１３　", "〒",
        )
        for postal in postal_forms:
            with self.subTest(postal=postal):
                page = ('<title>カラオケＣＬＵＢ ＤＡＭ &nbsp; カラオケＣＬＵＢ ＤＡＭ 黒磯店</title>'
                        f'?ll=36.965419,140.067155<dt>住所：</dt><dd>{postal}栃木県那須塩原市鍋掛1086-202</dd>')
                shop = module.parse_clubdam(url, page)
                self.assertEqual(shop["name"], "黒磯店")
                self.assertEqual(shop["prefecture"], "栃木県")
                self.assertEqual(shop["address"], "栃木県那須塩原市鍋掛1086-202")

    def test_utahiroba_page(self):
        url = "https://www.utahiro.com/shops/detail/1"
        markup = """<title> 新宿三丁目店｜歌広場</title>!2d139.7!3d35.6
            <div>住所</div></div><div><div>東京都新宿区新宿3-1-1</div>
            <div>営業時間</div></div><div><div>11:00～翌5:00</div>"""
        shop = module.parse_utahiroba(url, markup)
        self.assertEqual(shop["name"], "新宿三丁目店")
        self.assertEqual(shop["hours"], "11:00～翌5:00")
        self.assertEqual(shop["prefecture"], "東京都")

    def test_utahiroba_stops_at_the_404_after_the_last_page(self):
        class Page:
            def __init__(self, status_code, text=""):
                self.status_code, self.text = status_code, text

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise AssertionError("must not raise for the page past the end")

        pages = [Page(200, '<a href="/shops/detail/1">'), Page(404)]
        link = re.compile(r'href="(/shops/detail/\d+)"')
        with mock.patch.object(module, "request", side_effect=pages),                 mock.patch.object(module, "RequestPacer"),                 mock.patch.object(module, "UTAHIROBA_LINK", link),                 mock.patch.object(module, "details", side_effect=lambda urls, *a, **k: urls):
            self.assertEqual(module.utahiroba_shops(object(), [], 0),
                             [module.UTAHIROBA_BASE + "/shops/detail/1"])


if __name__ == "__main__":
    unittest.main()
