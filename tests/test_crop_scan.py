"""Тесты crop_scan.py.

Запуск: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "crop_scan.py"

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None


@unittest.skipIf(Image is None, "Pillow не установлен")
class CropScanCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.page = self.root / "page.png"
        Image.new("RGB", (400, 300), "white").save(self.page)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_script(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, encoding="utf-8")

    def test_crop_writes_three_files_and_reports_box(self):
        proc = self.run_script("--source", str(self.page), "--box", "10,20,110,80",
                               "--out-dir", str(self.root / "out"), "--prefix", "p1-rows")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["box"], [10, 20, 110, 80])
        self.assertEqual(result["page_size"], [400, 300])
        for key in ("context", "native", "enhanced"):
            self.assertTrue(Path(result["outputs"][key]).is_file(), key)
        self.assertEqual(Image.open(result["outputs"]["native"]).size, (100, 60))
        self.assertEqual(Image.open(result["outputs"]["enhanced"]).size, (150, 90))

    def test_bad_box_is_rejected(self):
        for box in ("10,20,5,80", "0,0,500,10", "1,2,3"):
            proc = self.run_script("--source", str(self.page), "--box", box,
                                   "--out-dir", str(self.root / "out"), "--prefix", "x")
            self.assertEqual(proc.returncode, 2, box)
        self.assertFalse((self.root / "out" / "x-native.png").exists())


if __name__ == "__main__":
    unittest.main()
