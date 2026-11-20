"""docs/proposal/index.html 을 A4 가로 PDF 로 찍는다.

    uv run --with playwright python build/make_pdf.py   # docs/proposal/기획서.pdf
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "proposal" / "index.html"
OUT = ROOT / "docs" / "proposal" / "기획서.pdf"

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page()
    pg.goto(SRC.as_uri(), wait_until="networkidle")
    pg.evaluate("document.fonts.ready")
    pg.pdf(path=str(OUT), prefer_css_page_size=True, print_background=True)
    b.close()
print(OUT.relative_to(ROOT))
