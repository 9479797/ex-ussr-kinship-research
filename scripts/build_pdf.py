#!/usr/bin/env python3
"""Build the human-readable PDF report from an HTML file via headless Chrome/Chromium."""

from __future__ import annotations

import argparse
import html.parser
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SERVICE_PATTERNS = [
    (re.compile(r"\b[ACPRS]-\d{4}\b"), "идентификатор реестра (A-/C-/P-/R-/S-NNNN)"),
    (re.compile(r"\bIG-[\w-]+"), "independence_group"),
    (
        re.compile(
            r"\b(strongly-corroborated|single-source|corroborated|hypothesis|unresolved|contradicted|lead-only)\b"
        ),
        "английский статус из реестра",
    ),
    (
        re.compile(r"\b(artifact_id|claim_id|person_id|relationship_id|independence_group)\b"),
        "имя поля реестра",
    ),
    (re.compile(r"<[^<>\n]{1,300}>"), "незаполненный плейсхолдер <…>"),
]

CHROME_NAMES = [
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "chrome",
    "msedge",
    "microsoft-edge",
]
CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]


class VisibleText(html.parser.HTMLParser):
    """Collect visible text and image sources; comments, <style> and <script> are skipped."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.chunks: list[str] = []
        self.images: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("style", "script", "title"):
            self.skip += 1
        if tag == "img":
            src = dict(attrs).get("src") or ""
            self.images.append(src)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("style", "script", "title") and self.skip:
            self.skip -= 1

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.chunks.append(data)

    def unknown_decl(self, data: str) -> None:  # pragma: no cover - defensive
        pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Собирает читаемую PDF-версию отчёта из HTML (SKILL.md, шаг 12)."
    )
    parser.add_argument("--html", required=True, help="HTML-файл отчёта (обычно <дело>/pdf-source/report.html)")
    parser.add_argument("--out", help="Путь к PDF; по умолчанию рядом с HTML")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Только проверить текст и изображения, PDF не собирать",
    )
    parser.add_argument(
        "--allow-service-ids",
        action="store_true",
        help="Не останавливаться на служебных идентификаторах (для рабочей версии)",
    )
    parser.add_argument("--chrome", help="Путь к Chrome/Chromium/Edge; иначе переменная CHROME_PATH или автопоиск")
    parser.add_argument(
        "--png-dir",
        help="Отрисовать страницы PDF в PNG для визуальной проверки (нужен установленный pypdfium2)",
    )
    return parser.parse_args()


def check_html(html_path: Path) -> tuple[list[str], list[str]]:
    parser = VisibleText()
    parser.feed(html_path.read_text(encoding="utf-8"))
    text = re.sub(r"\s+", " ", "".join(parser.chunks))

    problems: list[str] = []
    for pattern, label in SERVICE_PATTERNS:
        hits = sorted(set(m.group(0) for m in pattern.finditer(text)))
        if hits:
            problems.append(f"{label}: {', '.join(hits[:10])}")

    missing: list[str] = []
    for src in parser.images:
        if not src or "<" in src:
            missing.append(src or "<пустой src>")
            continue
        if re.match(r"^(https?:|data:)", src):
            continue
        if not (html_path.parent / src).is_file():
            missing.append(src)
    return problems, missing


def find_chrome(explicit: str | None) -> str | None:
    for candidate in (explicit, os.environ.get("CHROME_PATH")):
        if candidate and Path(candidate).exists():
            return candidate
    for name in CHROME_NAMES:
        found = shutil.which(name)
        if found:
            return found
    for path in CHROME_PATHS:
        if Path(path).exists():
            return path
    return None


def print_pdf(chrome: str, html_path: Path, out_path: Path) -> int:
    uri = html_path.resolve().as_uri()
    with tempfile.TemporaryDirectory(prefix="kinship-pdf-") as profile:
        base = [
            chrome,
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={profile}",
            "--no-pdf-header-footer",
            f"--print-to-pdf={out_path}",
            uri,
        ]
        for headless in ("--headless=new", "--headless"):
            if out_path.exists():
                out_path.unlink()
            result = subprocess.run(
                [base[0], headless, *base[1:]],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=180,
            )
            if out_path.is_file() and out_path.stat().st_size > 0:
                return 0
        sys.stderr.write(result.stderr.decode("utf-8", "replace")[-2000:])
    return 3


def count_pages(pdf_path: Path) -> int:
    return len(re.findall(rb"/Type\s*/Page[^s]", pdf_path.read_bytes()))


def render_png(pdf_path: Path, png_dir: Path) -> None:
    try:
        import pypdfium2 as pdfium  # type: ignore
    except ImportError:
        print(
            "pypdfium2 не установлен: страницы не отрисованы. Просмотрите PDF другим способом "
            "(встроенный просмотр PDF агента, pdftoppm, системный просмотрщик)."
        )
        return
    png_dir.mkdir(parents=True, exist_ok=True)
    pdf = pdfium.PdfDocument(str(pdf_path))
    for index in range(len(pdf)):
        pdf[index].render(scale=1.3).to_pil().save(png_dir / f"page{index + 1}.png")
    print(f"Страницы отрисованы: {png_dir} ({len(pdf)} шт.)")


def main() -> int:
    args = parse_args()
    html_path = Path(args.html).expanduser().resolve()
    if not html_path.is_file():
        print(f"Ошибка: нет файла {html_path}", file=sys.stderr)
        return 2

    problems, missing = check_html(html_path)
    if missing:
        print("Ошибка: не найдены изображения: " + ", ".join(missing), file=sys.stderr)
    if problems:
        stream = sys.stdout if args.allow_service_ids else sys.stderr
        print("В видимом тексте найдены служебные данные:", file=stream)
        for problem in problems:
            print(f"- {problem}", file=stream)
    if missing or (problems and not args.allow_service_ids):
        return 1
    if args.check_only:
        print("Проверка пройдена.")
        return 0

    chrome = find_chrome(args.chrome)
    if not chrome:
        print(
            "Ошибка: не найден Chrome, Chromium или Edge. Укажите путь через --chrome или CHROME_PATH.",
            file=sys.stderr,
        )
        return 2

    out_path = Path(args.out).expanduser().resolve() if args.out else html_path.with_suffix(".pdf")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    code = print_pdf(chrome, html_path, out_path)
    if code:
        print("Ошибка: браузер не смог напечатать PDF.", file=sys.stderr)
        return code

    print(f"PDF собран: {out_path} (страниц: {count_pages(out_path)}, {out_path.stat().st_size // 1024} КБ)")
    if args.png_dir:
        render_png(out_path, Path(args.png_dir).expanduser().resolve())
    print("Просмотрите каждую страницу перед отправкой пользователю (SKILL.md, шаг 12, п. 8).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
