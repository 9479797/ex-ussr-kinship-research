"""Ленты-обзоры: выбор кадров по номерам, вырезка графы и разбиение на ленты."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from scripts.scan_montage import frame_number, main, parse_box, select_frames

try:
    from PIL import Image, ImageDraw
except ImportError:  # Pillow — необязательная зависимость
    Image = None


class FrameSelection(unittest.TestCase):
    def test_frame_number_is_last_number_in_name(self):
        self.assertEqual(frame_number(Path("-archive-d1764-0007.jpg")), 7)
        self.assertEqual(frame_number(Path("00000116.jpg")), 116)
        self.assertIsNone(frame_number(Path("cover.jpg")))

    def test_range_filters_and_ignores_non_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("0001.jpg", "0002.jpg", "0003.png", "0004.jpg", "SHA256SUMS", "notes.txt"):
                (Path(tmp) / name).write_bytes(b"")
            names = [p.name for p in select_frames(Path(tmp), 2, 3)]
            self.assertEqual(names, ["0002.jpg", "0003.png"])

    def test_box_validation(self):
        self.assertEqual(parse_box("0.1,0.2,0.5,0.6"), (0.1, 0.2, 0.5, 0.6))
        for bad in ("0.5,0.2,0.1,0.6", "0,0,1", "0,0,1.5,1"):
            with self.subTest(bad=bad), self.assertRaises(Exception):
                parse_box(bad)


@unittest.skipIf(Image is None, "Pillow не установлен")
class Montage(unittest.TestCase):
    def make_frames(self, folder: Path, count: int, landscape: bool):
        size = (400, 200) if landscape else (200, 400)
        for i in range(1, count + 1):
            image = Image.new("L", size, 255)
            ImageDraw.Draw(image).rectangle((size[0] - 60, 20, size[0] - 10, 40), fill=0)
            image.save(folder / f"{i:04d}.jpg")

    def test_sheets_are_split_and_labelled_by_frame_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, out = Path(tmp, "src"), Path(tmp, "out")
            src.mkdir()
            self.make_frames(src, 5, landscape=True)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = main([str(src), "--out", str(out), "--box", "0,0,1,0.5",
                             "--half", "auto", "--per-sheet", "2", "--width", "300"])
            self.assertEqual(code, 0)
            names = sorted(p.name for p in out.iterdir())
            self.assertEqual(names, ["strip-0001-0002.jpg", "strip-0003-0004.jpg", "strip-0005-0005.jpg"])
            with Image.open(out / "strip-0001-0002.jpg") as sheet:
                self.assertEqual(sheet.size[0], 370)

    def test_empty_range_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            self.make_frames(src, 2, landscape=False)
            with contextlib.redirect_stderr(io.StringIO()):
                code = main([str(src), "--out", str(src / "o"), "--box", "0,0,1,1", "--first", "9"])
            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
