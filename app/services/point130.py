"""130point.com sold-comp source — real recent sales, incl. hidden best offers.

Why this source exists: when an eBay sale closes via *Best Offer accepted*, eBay
hides the actual accepted amount on the public completed listing (it shows the
original ask with a "Best offer accepted" tag, not the dollar figure paid).
130point surfaces the *real* accepted amount, so on slower-moving cards — where
the market actually clears below ask — it captures sales the Insights API and
SportsCardsPro can't see. That makes it additive, not redundant.

How it works: 130point's site posts the query to a backend search endpoint and
renders the results as HTML. We do the same single POST, then parse. Parsing is
isolated in `parse_results_html` so it can be unit-tested against a recorded
fixture without network access. The markup is not a public API and can change;
selectors are kept permissive and any failure degrades to an empty list — the
caller then reports "no comps" rather than inventing a price.

ToS-gray (like the eBay scrapers): OFF by default, gated by POINT130_ENABLED.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from urllib.parse import urljoin

import httpx
from selectolax.parser import HTMLParser

from app.config import get_settings
from app.services.ebay.base import SoldComp

logger = logging.getLogger("point130")

# Public site + its search backend (the page POSTs here under the hood).
_SITE = "https://130point.com/sales/"
_SEARCH_URL = "https://back.130point.com/sales/"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_PRICE_RE = re.compile(r"[\d,]+\.\d{2}")
# Accepts "Apr 12, 2026", "Apr 12 2026", and ISO "2026-04-12".
_DATE_RE = re.compile(r"([A-Z][a-z]{2})\s+(\d{1,2}),?\s+(\d{4})")
_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_GRADE_RE = re.compile(r"\b(PSA|BGS|SGC|CSG|CGC)\s*\d+(?:\.\d)?\b", re.IGNORECASE)
_BEST_OFFER_RE = re.compile(r"best\s*offer", re.IGNORECASE)
# 130point pools sales from several venues; detect which one each row came from
# so we can record the original marketplace alongside the 130point provider.
_MARKETPLACES = [
    ("eBay", re.compile(r"\bebay\b", re.IGNORECASE)),
    ("PWCC", re.compile(r"\bpwcc\b", re.IGNORECASE)),
    ("Goldin", re.compile(r"\bgoldin\b", re.IGNORECASE)),
    ("Heritage", re.compile(r"\bheritage\b", re.IGNORECASE)),
    ("MySlabs", re.compile(r"\bmyslabs\b", re.IGNORECASE)),
    ("Probstein", re.compile(r"\bprobstein\b", re.IGNORECASE)),
]


def _detect_marketplace(text: str | None) -> str | None:
    if not text:
        return None
    for name, rx in _MARKETPLACES:
        if rx.search(text):
            return name
    return None
_MONTHS = {
    m: i
    for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
        start=1,
    )
}


def is_enabled() -> bool:
    return bool(get_settings().point130_enabled)


def build_search_payload(query: str) -> dict[str, str]:
    """Form fields the 130point search backend expects.

    `type=2` is the eBay sold-sales search; `subcategory=0` = all. Kept here so
    the request shape is documented and easy to adjust if the site changes.
    """
    return {"query": query, "type": "2", "subcategory": "0"}


def _parse_price(text: str | None) -> float | None:
    if not text:
        return None
    m = _PRICE_RE.search(text.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def parse_sold_date(text: str | None) -> str | None:
    """Parse a sale date into an ISO string, accepting 'Apr 12, 2026' or ISO."""
    if not text:
        return None
    iso = _ISO_DATE_RE.search(text)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3))).isoformat()
        except ValueError:
            return None
    m = _DATE_RE.search(text)
    if not m:
        return None
    month = _MONTHS.get(m.group(1).title())
    if not month:
        return None
    try:
        return date(int(m.group(3)), month, int(m.group(2))).isoformat()
    except ValueError:
        return None


def _detect_grade(title: str | None) -> str | None:
    if not title:
        return None
    m = _GRADE_RE.search(title)
    return m.group(0).upper() if m else None


def parse_results_html(html: str) -> list[SoldComp]:
    """Extract sold comps from a 130point results page.

    Permissive by design: 130point renders each sale as a row/card with a title,
    a price, a date and a link. We try the known containers, then fall back to
    any element that carries a price, so a markup tweak degrades gracefully
    instead of throwing.
    """
    tree = HTMLParser(html)
    items = (
        tree.css(".cardInfo, .sale, .result, .salesItem, tr.sales, li.sales")
        or tree.css("tr")
    )
    comps: list[SoldComp] = []
    seen: set[tuple[str, float | None, str | None]] = set()
    for item in items:
        text = item.text(separator=" ", strip=True)
        price = _parse_price(text)
        if price is None:
            continue  # header rows / chrome carry no price

        link_node = item.css_first("a[href]")
        href = link_node.attributes.get("href") if link_node else None
        if href:
            href = urljoin(_SITE, href)

        # Prefer a dedicated title node; else the link text; else the row text.
        title_node = item.css_first(".title, .cardTitle, .itemTitle, h3, h4")
        title = (
            (title_node and title_node.text(strip=True))
            or (link_node and link_node.text(strip=True))
            or text
        )
        title = title.strip()
        if not title:
            continue

        img_node = item.css_first("img")
        thumb = (
            img_node.attributes.get("src") or img_node.attributes.get("data-src")
            if img_node
            else None
        )

        sold_date = parse_sold_date(text)
        # 130point's edge: it reports the real accepted amount on best-offer
        # sales. Tag the source so the UI/estimator can see where it came from.
        best_offer = bool(_BEST_OFFER_RE.search(text))
        source = "130point (sold, best offer)" if best_offer else "130point (sold)"
        # The original venue (eBay/PWCC/Goldin/...). Default to eBay: 130point's
        # type=2 search is its eBay sold feed, so unlabeled rows are eBay sales.
        marketplace = _detect_marketplace(text) or "eBay"

        key = (title, price, sold_date)
        if key in seen:
            continue
        seen.add(key)
        comps.append(
            SoldComp(
                title=title,
                sold_price=price,
                sold_date=sold_date,
                condition_grade=_detect_grade(title),
                listing_url=href,
                thumbnail_url=thumb,
                source=source,
                marketplace=marketplace,
                kind="sold",
            )
        )
    return comps


def fetch_sold_comps(query: str, *, graded: bool = False) -> list[SoldComp]:
    """Query 130point for recent sold comps. Returns [] on any failure."""
    if not is_enabled():
        return []
    try:
        resp = httpx.post(
            _SEARCH_URL,
            data=build_search_payload(query),
            headers={
                "User-Agent": _UA,
                "Accept-Language": "en-US,en;q=0.9",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": _SITE,
            },
            timeout=30,
            follow_redirects=True,
        )
        resp.raise_for_status()
    except Exception:  # noqa: BLE001
        logger.exception("130point search failed for %r", query)
        return []

    comps = parse_results_html(resp.text)
    if not comps:
        logger.warning("130point returned 0 parseable comps for %r", query)
    return comps
