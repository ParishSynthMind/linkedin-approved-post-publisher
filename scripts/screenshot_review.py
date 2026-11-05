"""Screenshot queue/review.html for the README (headless Chromium via Playwright).
Usage: python scripts/screenshot_review.py REVIEW_HTML OUT.png"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

src, out = Path(sys.argv[1]).resolve(), sys.argv[2]
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 900, "height": 900}, device_scale_factor=2)
    page.goto(src.as_uri())
    page.screenshot(path=out, full_page=True)
    browser.close()
