"""Тесты crop_scan.py.

Запуск: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import json
import hashlib
import shutil
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
        with Image.open(result["outputs"]["native"]) as native:
            self.assertEqual(native.size, (100, 60))
        with Image.open(result["outputs"]["enhanced"]) as enhanced:
            self.assertEqual(enhanced.size, (150, 90))

    def test_bad_box_is_rejected(self):
        for box in ("10,20,5,80", "0,0,500,10", "1,2,3", "-1,0,10,10", "a,0,10,10"):
            proc = self.run_script("--source", str(self.page), "--box", box,
                                   "--out-dir", str(self.root / "out"), "--prefix", "x")
            self.assertEqual(proc.returncode, 2, box)
        self.assertFalse((self.root / "out" / "x-native.png").exists())

    def test_existing_outputs_and_source_are_preserved(self):
        source = self.root / "x-native.png"
        source.write_bytes(self.page.read_bytes())
        before = source.read_bytes()
        proc = self.run_script("--source", str(source), "--box", "0,0,10,10",
                               "--out-dir", str(self.root), "--prefix", "x")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(source.read_bytes(), before)
        self.assertFalse((self.root / "x-context.jpg").exists())

    def test_prefix_cannot_escape_output_directory(self):
        proc = self.run_script("--source", str(self.page), "--box", "0,0,10,10",
                               "--out-dir", str(self.root / "out"), "--prefix", "../outside")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertFalse((self.root / "outside-native.png").exists())

    @unittest.skipUnless(shutil.which("pdftoppm"), "pdftoppm не установлен")
    def test_pdf_page_and_source_hash_are_recorded(self):
        source = self.root / "scan.pdf"
        Image.new("RGB", (400, 300), "white").save(source, "PDF")
        original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        proc = self.run_script("--source", str(source), "--page", "1", "--dpi", "72",
                               "--box", "10,20,110,80", "--out-dir", str(self.root / "out"), "--prefix", "pdf")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["page"], 1)
        self.assertEqual(result["dpi"], 72)
        self.assertEqual(result["source_sha256"], original_hash)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original_hash)


if __name__ == "__main__":
    unittest.main()
