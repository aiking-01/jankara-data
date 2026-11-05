"""GitHub Actions用の本物のTesseractを使う小さなOCR確認テスト。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import extract_banban as ocr
import run_banban_ocr as runner


class OcrSmokeTests(unittest.TestCase):
    def test_real_tesseract_reads_red_price(self):
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 42)
        canvas = Image.new("RGB", (155, 62), "white")
        ImageDraw.Draw(canvas).text((10, 2), "270", fill=(255, 0, 0), font=font)
        self.assertEqual(ocr.number(np.asarray(canvas), "red", "smoke"), 270)

    def test_rejects_untrusted_url_and_unrelated_paths(self):
        self.assertFalse(runner.allowed_url("http://raw.githubusercontent.com/a/b.png"))
        self.assertFalse(runner.allowed_url("https://localhost/admin"))
        self.assertTrue(runner.allowed_url("https://raw.githubusercontent.com/org/repo/main/weekday.png"))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                runner.image_path(str(Path(directory) / "outside.png"), "bb-00075", "weekday", Path(directory))


if __name__ == "__main__":
    unittest.main()
