"""Headless-browser scrape of eBay SOLD pages (best-effort, per-card, on demand).

A real Chromium browser (Playwright) executes JS and carries browser-like
headers/cookies/TLS, which gets past much of eBay's bot detection that blocks
plain HTTP requests. This is best-effort and ToS-gray: eBay may still serve a
CAPTCHA/challenge, and page markup changes over time. It returns real SOLD data
when it works and an empty list otherwise — never a fabricated price.

Disabled unless EBAY_BROWSER_SCRAPE_ENABLED=true AND Playwright + Chromium are
installed (`pip install playwright && playwright install chromium`).
"""
from __future__ import annotations

import logging

from app.config import get_settings
from app.services.ebay.base import SoldComp
from app.services.ebay.scrape import build_sold_search_url, parse_sold_html

logger = logging.getLogger("ebay.browser_scrape")

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def is_enabled() -> bool:
    return bool(get_settings().ebay_browser_scrape_enabled)


def _render_html(url: str) -> str | None:
    """Fetch a fully-rendered page with Playwright. Returns HTML or None."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001 — playwright not installed
        logger.warning("Playwright not installed; browser scrape unavailable.")
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=_UA, locale="en-US")
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                # Let listing tiles render.
                page.wait_for_selector(".s-item", timeout=15000)
                return page.content()
            finally:
                browser.close()
    except Exception:  # noqa: BLE001 — navigation/challenge/timeout
        logger.exception("Playwright render failed for %s", url)
        return None


def fetch_sold_comps(query: str, *, graded: bool = False) -> list[SoldComp]:
    if not is_enabled():
        return []
    q = f"{query} PSA 10" if graded else query
    html = _render_html(build_sold_search_url(q))
    if not html:
        return []
    comps = parse_sold_html(html)
    # These are genuine completed sales — tag them as sold and label the source.
    for c in comps:
        c.kind = "sold"
        c.source = "ebay (sold, scraped)"
    return comps
