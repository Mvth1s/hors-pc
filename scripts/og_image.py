"""Génère site/og-image.png (1200 x 630) avec les polices et les tokens de site/style.css.

Usage : uv run python scripts/og_image.py
L'image reprend le logo, l'accroche et les badges console du site (thème sombre).
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

SITE = Path(__file__).resolve().parents[1] / "site"
OUTPUT = SITE / "og-image.png"
TAGLINE = "Les jeux console absents du PC, au meilleur prix relevé en France."
CONSOLES = (("PS5", "ps"), ("Switch", "nin"), ("Switch 2", "nin"), ("Xbox Series", "xb"))

PAGE = """<!doctype html>
<html lang="fr" class="hp dark">
<head><meta charset="utf-8"><link rel="stylesheet" href="style.css">
<style>
  html, body {{ margin: 0; width: 1200px; height: 630px; overflow: hidden; }}
  .og {{ box-sizing: border-box; width: 1200px; height: 630px; padding: 80px 90px;
        background: var(--bg); color: var(--ink); display: flex; flex-direction: column; }}
  .og .logo {{ font-size: 132px; gap: 8px; }}
  .og .logo-pc {{ padding: 6px 26px 14px; border-radius: 24px; margin-left: 8px; }}
  .og .logo-pc::after {{ height: 9px; }}
  .og p {{ margin: 92px 0 0; max-width: 900px; font-size: 38px; line-height: 1.35;
          color: var(--muted); }}
  .og .consoles {{ margin-top: auto; display: flex; gap: 14px; }}
  .og .console {{ height: 50px; padding: 0 18px; border-radius: 10px; font-size: 24px; }}
</style></head>
<body class="hp dark"><div class="og">
  <div class="logo">Hors<span class="logo-pc">PC</span></div>
  <p>{tagline}</p>
  <div class="consoles">{consoles}</div>
</div></body></html>
"""


def main() -> int:
    consoles = "".join(f'<span class="console {cls}">{name}</span>' for name, cls in CONSOLES)
    page_path = SITE / "_og-image.html"  # à côté de style.css pour les chemins des polices
    page_path.write_text(PAGE.format(tagline=TAGLINE, consoles=consoles), encoding="utf-8")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1200, "height": 630})
            page.goto(page_path.as_uri())
            page.evaluate("document.fonts.ready")
            page.screenshot(path=str(OUTPUT))
            browser.close()
    finally:
        page_path.unlink(missing_ok=True)
    print(f"{OUTPUT} écrit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
