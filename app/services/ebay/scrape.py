"""Scrape eBay SOLD/completed search results for comparable sales.

Best-effort and polite (single request, identifies a UA). Parsing is isolated in
`parse_sold_html` so it can be unit-tested against a recorded fixture without
network access. eBay markup changes over time; selectors are kept permissive and
failures degrade to an empty list — the caller then reports "no comps" rather
than inventing a price.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from urllib.parse import urlencode

import httpx
from selectolax.parser import HTMLParser

from app.services.ebay.base import SoldComp

logger = logging.getLogger("ebay.scrape")

_PRICE_RE = re.compile(r"[\d,]+\.\d{2}")
_DATE_RE = re.compile(
    r"([A-Z][a-z]{2})\s+(\d{1,2}),?\s+(\d{4})"  # "Apr 12, 2026"
)
_GRADE_RE = re.compile(r"\b(PSA|BGS|SGC|CSG)\s*\d+(?:\.\d)?\b", re.IGNORECASE)
_MONTHS = {
    m: i
    for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
        start=1,
    )
}
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def build_sold_search_url(query: str) -> str:
    # LH_Sold=1 & LH_Complete=1 restrict to sold/completed listings.
    params = {"_nkw": query, "LH_Sold": "1", "LH_Complete": "1", "_ipg": "120"}
    return "https://www.ebay.com/sch/i.html?" + urlencode(params)


def _parse_price(text: str | None) -> float | None:
    if not text:
        return None
    # Ranges like "$10.00 to $25.00" -> take the first price.
    m = _PRICE_RE.search(text.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def parse_sold_date(text: str | None) -> str | None:
    """Parse eBay's 'Sold  Apr 12, 2026' caption into an ISO date string."""
    if not text:
        return None
    m = _DATE_RE.search(text)
    if not m:
        return None
    mon, day, year = m.group(1), int(m.group(2)), int(m.group(3))
    month = _MONTHS.get(mon.title())
    if not month:
        return None
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _detect_grade(title: str | None) -> str | None:
    if not title:
        return None
    m = _GRADE_RE.search(title)
    return m.group(0).upper() if m else None


def parse_sold_html(html: str) -> list[SoldComp]:
    tree = HTMLParser(html)
    comps: list[SoldComp] = []
    for item in tree.css("li.s-item, div.s-item"):
        title_node = item.css_first(".s-item__title")
        price_node = item.css_first(".s-item__price")
        link_node = item.css_first("a.s-item__link")
        img_node = item.css_first(".s-item__image img, .s-item__image-wrapper img")
        # eBay shows the sold date in a caption row.
        date_node = item.css_first(".s-item__caption, .s-item__caption--row, .POSITIVE")

        title = title_node.text(strip=True) if title_node else None
        if not title or title.lower().startswith("shop on ebay"):
            continue
        price = _parse_price(price_node.text(strip=True) if price_node else None)
        href = link_node.attributes.get("href") if link_node else None
        thumb = (
            img_node.attributes.get("src") or img_node.attributes.get("data-src")
            if img_node
            else None
        )
        date_text = date_node.text(strip=True) if date_node else None
        comps.append(
            SoldComp(
                title=title,
                sold_price=price,
                sold_date=parse_sold_date(date_text) or date_text,
                condition_grade=_detect_grade(title),
                listing_url=href,
                thumbnail_url=thumb,
                source="ebay",
                marketplace="eBay",
            )
        )
    return comps


def fetch_sold_comps(query: str) -> list[SoldComp]:
    url = build_sold_search_url(query)
    try:
        resp = httpx.get(
            url,
            headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"},
            timeout=30,
            follow_redirects=True,
        )
        resp.raise_for_status()
    except Exception:  # noqa: BLE001
        logger.exception("eBay sold scrape failed for %r", query)
        return []
    comps = parse_sold_html(resp.text)
    if not comps:
        logger.warning("eBay scrape returned 0 parseable comps for %r", query)
    return comps
