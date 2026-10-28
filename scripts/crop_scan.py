#!/usr/bin/env python3
"""Cut a region of a scan for human reading: context page with the region outlined, native crop and an enhanced copy."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from research_common import SourceError, configure_utf8_stdio

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageOps
except ImportError:  # pragma: no cover - reported to the caller
    Image = None


def parse_box(value: str) -> tuple[int, int, int, int]:
    try:
        parts = [int(part) for part in value.replace(";", ",").split(",")]
    except ValueError as exc:
        raise SourceError("--box ожидает четыре целых числа x0,y0,x1,y1.") from exc
    if (len(parts) != 4 or min(parts) < 0
            or parts[0] >= parts[2] or parts[1] >= parts[3]):
        raise SourceError(f"--box ожидает x0,y0,x1,y1 в пикселях исходника, получено {value!r}.")
    return parts[0], parts[1], parts[2], parts[3]


def render_pdf_page(pdf: Path, page: int, dpi: int, workdir: Path) -> Path:
    if not shutil.which("pdftoppm"):
        raise SourceError("Для PDF нужен pdftoppm (poppler); либо передайте уже извлечённое изображение страницы.")
    prefix = workdir / "page"
    subprocess.run(
        ["pdftoppm", "-r", str(dpi), "-f", str(page), "-l", str(page), "-png", str(pdf), str(prefix)],
        check=True, capture_output=True,
    )
    rendered = sorted(workdir.glob("page*.png"))
    if not rendered:
        raise SourceError(f"pdftoppm не создал изображение страницы {page} из {pdf.name}.")
    return rendered[0]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def crop(args: argparse.Namespace) -> dict:
    if Image is None:
        raise SourceError("Нужен пакет Pillow: pip install pillow")
    source = Path(args.source).expanduser().resolve()
    if not source.is_file():
        raise SourceError(f"Файл не найден: {source}")
    if (not args.prefix or args.prefix in (".", "..")
            or "/" in args.prefix or "\\" in args.prefix):
        raise SourceError("--prefix должен быть именем файла без пути.")
    if (not math.isfinite(args.scale) or args.scale <= 0
            or args.context_width <= 0 or args.dpi <= 0 or args.page < 0):
        raise SourceError("Масштаб, ширина и DPI должны быть положительными; номер страницы — неотрицательным.")
    out_dir = Path(args.out_dir).expanduser().resolve()
    paths = {
        "context": out_dir / f"{args.prefix}-context.jpg",
        "native": out_dir / f"{args.prefix}-native.png",
        "enhanced": out_dir / f"{args.prefix}-enhanced.png",
    }
    for path in paths.values():
        if path.exists() or path.is_symlink():
            raise SourceError(f"Файл уже существует: {path}. Выберите другой --prefix.")
    box = parse_box(args.box)
    source_hash = sha256(source)
    with tempfile.TemporaryDirectory() as tmp:
        image_path = source
        if source.suffix.lower() == ".pdf":
            if not args.page:
                raise SourceError("Для PDF укажите --page.")
            image_path = render_pdf_page(source, args.page, args.dpi, Path(tmp))
        with Image.open(image_path) as image:
            page_image = image.convert("RGB")
    width, height = page_image.size
    if box[2] > width or box[3] > height:
        raise SourceError(f"--box выходит за пределы изображения {width}x{height}.")

    context = page_image.copy()
    ImageDraw.Draw(context).rectangle(box, outline="red", width=max(4, width // 500))
    if context.width > args.context_width:
        ratio = args.context_width / context.width
        context = context.resize((args.context_width, max(1, int(context.height * ratio))), Image.LANCZOS)
    region = page_image.crop(box)
    enhanced = ImageOps.autocontrast(region, cutoff=1).filter(ImageFilter.UnsharpMask(radius=3, percent=180, threshold=2))
    if args.scale != 1:
        enhanced = enhanced.resize((max(1, int(enhanced.width * args.scale)), max(1, int(enhanced.height * args.scale))), Image.LANCZOS)

    out_dir.mkdir(parents=True, exist_ok=True)
    for key, image in (("context", context), ("native", region), ("enhanced", enhanced)):
        with paths[key].open("xb") as output:
            image.save(output, format="JPEG" if key == "context" else "PNG", **({"quality": 85} if key == "context" else {}))
    return {
        "source": str(source),
        "source_sha256": source_hash,
        "page": args.page or None,
        "dpi": args.dpi if source.suffix.lower() == ".pdf" else None,
        "page_size": [width, height],
        "box": list(box),
        "outputs": {key: str(path) for key, path in paths.items()},
        "note": "Обработка может создавать визуальные артефакты; чтение сверять с native-копией и оригиналом.",
    }


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Вырезка области скана для чтения человеком: страница с рамкой, вырезка в исходном разрешении "
        "и копия с усиленным контрастом. Координаты — пиксели исходного изображения (для PDF — после рендера с --dpi)."
    )
    parser.add_argument("--source", required=True, help="PNG/JPG страницы или PDF")
    parser.add_argument("--page", type=int, default=0, help="Номер страницы PDF (с 1)")
    parser.add_argument("--dpi", type=int, default=300, help="Разрешение рендера PDF")
    parser.add_argument("--box", required=True, help="x0,y0,x1,y1 в пикселях")
    parser.add_argument("--out-dir", required=True, help="Папка дела для вырезок, например sources/<шифр>/")
    parser.add_argument("--prefix", required=True, help="Префикс имён файлов, например spr1547-p35-rows")
    parser.add_argument("--scale", type=float, default=1.5, help="Увеличение усиленной копии")
    parser.add_argument("--context-width", type=int, default=1400, help="Ширина страницы-контекста")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        result = crop(args)
    except (SourceError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    configure_utf8_stdio()
    raise SystemExit(main())
