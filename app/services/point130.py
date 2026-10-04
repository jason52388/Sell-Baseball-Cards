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
import time
from datetime import date
from urllib.parse import urljoin

import httpx
from selectolax.parser import HTMLParser

from app.config import get_settings
from app.services.ebay.base import SoldComp
from app.services.matching import GRADE_RE, detect_grade

logger = logging.getLogger("point130")

# Public site + its search backend (the page POSTs here under the hood).
_SITE = "https://130point.com/sales/"
_SEARCH_URL = "https://back.130point.com/sales/"
# Pause before re-asking after an empty page (see fetch_sold_comps).
_EMPTY_RETRY_DELAY = 1.5

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_PRICE_RE = re.compile(r"[\d,]+\.\d{2}")
# Accepts "Apr 12, 2026", "Apr 12 2026", and ISO "2026-04-12".
_DATE_RE = re.compile(r"([A-Z][a-z]{2})\s+(\d{1,2}),?\s+(\d{4})")
# The live rows use a day-first form with a weekday prefix:
# "Date: Thu 20 Aug 2026 03:34:35 GMT". Without this every comp is undated, and
# an undated sold comp silently bypasses the COMP_RECENCY_DAYS window.
_DAY_FIRST_DATE_RE = re.compile(r"\b(\d{1,2})\s+([A-Z][a-z]{2})[a-z]*\s+(\d{4})\b")
_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
# One grade pattern for every source (see matching.GRADE_RE).
_GRADE_RE = GRADE_RE
# Every row carries the literal text "Best Offer Price: <n>", so the phrase alone
# is present on 100% of sales. Only a NONZERO amount means an offer was accepted.
_BEST_OFFER_AMOUNT_RE = re.compile(
    r"best\s*offer\s*price\s*:?\s*\$?([\d,]+(?:\.\d{1,2})?)", re.IGNORECASE
)
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

    The current sales page posts `query`, `sort`, `tab_id`, `tz` and `mp` (all
    marketplaces); older versions of the page posted `type=2` (eBay sold
    search) and `subcategory=0`. Both sets are sent: the backend ignores fields
    it does not use, and a page change in either direction keeps working. Kept
    here so the request shape is documented and easy to adjust (verify with
    tools/verify_130point.py).
    """
    return {
        "query": query,
        "sort": "EndTimeSoonest",
        "tab_id": "1",
        "tz": "America/New_York",
        "mp": "all",
        "type": "2",
        "subcategory": "0",
    }


class Point130Error(RuntimeError):
    """130point could not answer. `state` is the source-status code
    comp_sources records: "blocked" (Cloudflare challenge, 403/429/503, or an
    empty stub response) or "error" (network failure)."""

    state = "error"

    def __init__(self, message: str, *, state: str | None = None):
        super().__init__(message)
        if state:
            self.state = state


# Markers of a bot-check / challenge page instead of results.
_BLOCK_MARKERS = (
    "just a moment", "cf-browser-verification", "challenge-platform", "cf_chl",
    "attention required", "cf-error-details", "access denied", "captcha",
)
_BLOCK_STATUSES = {403, 429, 503}
# A real results page (even one with no matching sales) is a full HTML page;
# a body this small with no rows is the stub the backend serves when it refuses.
_STUB_BODY_BYTES = 300


def classify_response(status: int, body: str) -> str | None:
    """Why a response is not a results page ("blocked: ..."), or None."""
    if status in _BLOCK_STATUSES:
        return f"130point refused the request (HTTP {status})"
    low = (body or "")[:5000].lower()
    for marker in _BLOCK_MARKERS:
        if marker in low:
            return "130point served a bot-check page instead of results"
    return None


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

    # Day-first ("20 Aug 2026") is tried first: a weekday prefix would otherwise
    # let the month-first pattern read "Thu 20" as a month and day.
    day_first = _DAY_FIRST_DATE_RE.search(text)
    if day_first:
        month = _MONTHS.get(day_first.group(2).title())
        if month:
            try:
                return date(
                    int(day_first.group(3)), month, int(day_first.group(1))
                ).isoformat()
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


def _is_best_offer_sale(text: str) -> bool:
    """True only when a best offer was actually accepted.

    Rows state "Best Offer Price: 0" on ordinary fixed-price sales, so matching
    the phrase tagged every sale as a best offer. When the amount is present we
    trust it; only when no amount is stated do we fall back to the phrase.
    """
    m = _BEST_OFFER_AMOUNT_RE.search(text)
    if m:
        try:
            return float(m.group(1).replace(",", "")) > 0
        except ValueError:
            return False
    return bool(_BEST_OFFER_RE.search(text))


def _detect_grade(title: str | None) -> str | None:
    return detect_grade(title)


# Price labels seen on live rows; the sale price must win over the list price,
# best-offer field and shipping that share the row.
_SALE_PRICE_RE = re.compile(
    r"(?:sale|sold)\s*price\s*:?\s*\$?\s*([\d,]+\.\d{2})", re.IGNORECASE
)


def _row_price(item, text: str) -> tuple[float | None, str | None]:
    """(price, currency) for a result row. Prefers the row's data-price
    attribute, then a "Sale Price:" label, then the first price in the text."""
    attrs = item.attributes or {}
    currency = (attrs.get("data-currency") or "").strip().upper() or None
    raw = attrs.get("data-price")
    if raw:
        try:
            return float(str(raw).replace(",", "").replace("$", "")), currency
        except ValueError:
            pass
    m = _SALE_PRICE_RE.search(text)
    if m:
        try:
            return float(m.group(1).replace(",", "")), currency
        except ValueError:
            pass
    return _parse_price(text), currency


def _row_title(item, text: str) -> str:
    node = item.css_first(
        "#titleText, .titleText, .title, .cardTitle, .itemTitle, h3, h4"
    )
    if node is not None and node.text(strip=True):
        return node.text(separator=" ", strip=True)
    # The first link is often the image link with no text; use the first one
    # that actually carries words.
    for link in item.css("a[href]"):
        words = link.text(strip=True)
        if words:
            return words
    return text


def parse_results_html(html: str) -> list[SoldComp]:
    """Extract sold comps from a 130point results page.

    Permissive by design: 130point renders each sale as a table row
    (`<tr id="dRow" data-price=".." data-currency="USD">` with title, date and
    link spans). We try the known containers, then fall back to any row that
    carries a price, so a markup tweak degrades gracefully instead of
    throwing. Non-USD sales are skipped (prices would not be comparable).
    """
    tree = HTMLParser(html)
    items = (
        tree.css("tr[data-price], .cardInfo, .sale, .result, .salesItem, tr.sales, li.sales")
        or tree.css("tr")
    )
    comps: list[SoldComp] = []
    seen: set[tuple[str, float | None, str | None]] = set()
    for item in items:
        text = item.text(separator=" ", strip=True)
        price, currency = _row_price(item, text)
        if price is None:
            continue  # header rows / chrome carry no price
        if currency and currency != "USD":
            continue

        link_node = None
        for link in item.css("a[href]"):
            link_node = link
            if link.text(strip=True):
                break
        href = link_node.attributes.get("href") if link_node else None
        if href:
            href = urljoin(_SITE, href)

        title = _row_title(item, text).strip()
        if not title:
            continue

        img_node = item.css_first("img")
        thumb = (
            img_node.attributes.get("src") or img_node.attributes.get("data-src")
            if img_node
            else None
        )

        date_node = item.css_first("#dateText, .dateText")
        sold_date = parse_sold_date(date_node.text(strip=True) if date_node else None)
        if sold_date is None:
            sold_date = parse_sold_date(text)
        # 130point's edge: it reports the real accepted amount on best-offer
        # sales. Tag the source so the UI/estimator can see where it came from.
        best_offer = _is_best_offer_sale(text)
        source = "130point (sold, best offer)" if best_offer else "130point (sold)"
        # The original venue (eBay/PWCC/Goldin/...). Default to eBay: 130point's
        # sold search is mostly its eBay feed, so unlabeled rows are eBay sales.
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


def _search(q: str) -> str:
    """One search POST. Returns the response body.

    Raises Point130Error("blocked") for a refusal or challenge page and
    Point130Error("error") for a network failure, so the caller reports the
    source as failing rather than as "no sales".
    """
    try:
        resp = httpx.post(
            _SEARCH_URL,
            data=build_search_payload(q),
            headers={
                "User-Agent": _UA,
                "Accept-Language": "en-US,en;q=0.9",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": "https://130point.com",
                "Referer": _SITE,
            },
            timeout=30,
            follow_redirects=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("130point search failed for %r", q)
        raise Point130Error(f"130point request failed: {exc}") from exc
    # Status codes only here; the page-content check runs only when a page
    # yields no rows (a results page may mention "captcha" in a script tag).
    blocked = classify_response(resp.status_code, "")
    if blocked:
        logger.warning("%s for %r", blocked, q)
        raise Point130Error(blocked, state="blocked")
    if resp.status_code >= 400:
        raise Point130Error(f"130point answered HTTP {resp.status_code}")
    return resp.text


def fetch_sold_comps(query: str, *, graded: bool = False) -> list[SoldComp]:
    """Query 130point for recent sold comps.

    Returns [] for a genuine "no sales" page. Raises Point130Error when the
    site refused or could not be reached (see `_search`), and when it keeps
    answering with an empty stub instead of a results page.

    Retries once on an empty page. Observed live: the first request after an
    idle period answers 200 with a ~114-byte body and no rows, while the same
    query moments later returns the full results page. Without the retry the
    first card priced after any pause silently loses this source.
    """
    if not is_enabled():
        return []
    # Same convention as the eBay sources: the graded pass asks for slabbed
    # sales. Without this the "graded" results were raw sales.
    q = f"{query} PSA 10" if graded else query

    body = ""
    for attempt in range(2):
        body = _search(q)
        comps = parse_results_html(body)
        if comps:
            return comps
        blocked = classify_response(200, body)
        if blocked:
            logger.warning("%s for %r", blocked, q)
            raise Point130Error(blocked, state="blocked")
        if attempt == 0:
            logger.info("130point returned an empty page for %r; retrying once", q)
            time.sleep(_EMPTY_RETRY_DELAY)

    size = len((body or "").encode("utf-8"))
    if size < _STUB_BODY_BYTES:
        logger.warning("130point answered a %d-byte stub for %r (after retry)", size, q)
        raise Point130Error(
            f"130point returned an empty {size}-byte response instead of results",
            state="blocked",
        )
    logger.info("130point returned 0 parseable comps for %r (after retry)", q)
    return []
