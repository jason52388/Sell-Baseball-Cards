"""SportsCardsPro / PriceCharting provider — real card market/sold-derived prices.

Sports cards live on SportsCardsPro.com (a PriceCharting property); the same
account token works there. pricecharting.com itself indexes games/Funko/Marvel,
NOT sports cards — so for baseball cards we query the SportsCardsPro base.
Docs: https://www.sportscardspro.com/api-documentation

Two-step lookup for accuracy:
  1. GET /api/products?q=...  -> a LIST of candidate products
  2. pick the candidate whose name actually matches the card (year + set +
     player), preferring the plainest match unless a parallel was specified,
     then GET /api/product?id=... for its prices.

We do NOT trust /api/product?q= (single best guess). If no candidate genuinely
matches, we return nothing rather than a wrong price.

What this provider can and cannot give:
  - AGGREGATE price points per grade (the JSON product endpoint). One number per
    tier — NOT individual sales. `fetch_comps` (ungraded/PSA 10, drives the
    estimate) and `fetch_grade_tiers` (every tier, informational) use these.
  - INDIVIDUAL dated sales are NOT in the API. They live only on the product web
    page's "recent sales" table, so `fetch_individual_sales` scrapes that page
    (ToS-gray, opt-in via SPORTSCARDSPRO_SALES_ENABLED).

Sports-card price fields (in pennies) -> grade label. Only the tiers below are
mapped; ambiguous fields (new/complete/box-only) are intentionally omitted so we
never label a price with the wrong grade. Verify field names with
`python -m tools.verify_sportscardspro "<card>" --raw`.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import date
from urllib.parse import quote_plus, urljoin

import httpx
from selectolax.parser import HTMLParser

from app.config import get_settings
from app.services.ebay.base import SoldComp

# Short-lived cache of fetched product-page HTML, keyed by URL. The image scrape
# and the sales-history scrape hit the SAME page, so this avoids fetching the
# (large) page twice for one card.
_PAGE_TTL = 600  # seconds
_page_cache: dict[str, tuple[float, str]] = {}
_page_lock = threading.Lock()


def _get_product_page(url: str) -> str | None:
    now = time.monotonic()
    hit = _page_cache.get(url)
    if hit is not None and now - hit[0] < _PAGE_TTL:
        return hit[1]
    try:
        resp = httpx.get(
            url,
            headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"},
            timeout=30,
            follow_redirects=True,
        )
        resp.raise_for_status()
    except Exception:  # noqa: BLE001
        logger.warning("SportsCardsPro product-page fetch failed (%s)", url)
        return None
    with _page_lock:
        _page_cache[url] = (now, resp.text)
    return resp.text

logger = logging.getLogger("pricecharting")

# field name -> (grade label, is_ungraded). Order = display order.
_TIERS: list[tuple[str, str, bool]] = [
    ("loose-price", "Ungraded", True),
    ("graded-price", "PSA 9", False),
    ("manual-only-price", "PSA 10", False),
    ("bgs-10-price", "BGS 10", False),
    ("condition-17-price", "SGC 10", False),
    ("condition-18-price", "CGC 10", False),
]

_PRICE_RE = re.compile(r"[\d,]+\.\d{2}")
_DATE_RE = re.compile(r"([A-Z][a-z]{2})\s+(\d{1,2}),?\s+(\d{4})")
_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_GRADE_RE = re.compile(r"\b(PSA|BGS|SGC|CSG|CGC)\s*\d+(?:\.\d)?\b", re.IGNORECASE)
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


def _base() -> str:
    return get_settings().cardpricing_api_base.rstrip("/")


def has_token() -> bool:
    return bool(get_settings().pricecharting_token)


def _dollars(pennies) -> float | None:
    try:
        cents = int(pennies)
    except (TypeError, ValueError):
        return None
    return round(cents / 100.0, 2) if cents > 0 else None


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _product_title(p: dict) -> str:
    return f"{p.get('console-name', '')} {p.get('product-name', '')}".strip()


# Generic words inside a parallel/insert name that we should NOT require verbatim
# in the candidate title (they rarely appear on the product page).
_PARALLEL_STOPWORDS = {
    "insert", "parallel", "variation", "card", "base", "sp", "ssp", "rc",
    "the", "and", "of", "a", "an",
}


def _required_parallel_tokens(parallel: str | None) -> set[str]:
    """The distinguishing tokens of a parallel/insert that a candidate product
    MUST contain to be considered the same card (e.g. {"global", "impact"})."""
    if not parallel:
        return set()
    return {t for t in _tokens(parallel) if len(t) > 2 and t not in _PARALLEL_STOPWORDS}


def select_best_product(
    products: list[dict],
    query: str,
    *,
    require_parallel: str | None = None,
    require_number: str | None = None,
) -> dict | None:
    """Pick the product that genuinely matches the query, or None.

    Requires the query's year (if any) to appear in the candidate and a
    reasonable token overlap — so unrelated products are rejected.

    Disambiguation, strongest first:
      - `require_number`: if the card has a printed number, the candidate MUST
        carry that number. The number is authoritative — a subset/insert is
        often catalogued under the base set with only its number (e.g. a "Global
        Impact" card listed as plain "#189"), so when we have the number we
        match on it and do NOT also demand the parallel name.
      - `require_parallel`: only when there's NO number to pin the card down, the
        candidate must contain the parallel/insert tokens. This stops us silently
        pricing the BASE card when the real insert isn't catalogued — we return
        None (→ flag for review) instead of a wrong price.
    """
    qtokens = _tokens(query)
    if not qtokens:
        return None
    years = {t for t in qtokens if len(t) == 4 and t.isdigit()}
    needed = max(3, (len(qtokens) + 1) // 2)
    req_parallel = _required_parallel_tokens(require_parallel)
    number = str(require_number).lstrip("#").strip() if require_number else ""
    number_re = re.compile(rf"#?\b{re.escape(number)}\b") if number else None

    best: dict | None = None
    best_key = (0, 0)  # (overlap, -extra_tokens) — higher overlap, fewer extras
    for p in products:
        title_norm = _product_title(p).lower()
        ttokens = _tokens(_product_title(p))
        if years and not (years & ttokens):
            continue  # wrong/!missing year -> not this card
        if number_re is not None:
            if not number_re.search(title_norm):
                continue  # has a number but this candidate isn't it
        elif req_parallel and not req_parallel.issubset(ttokens):
            continue  # no number to disambiguate + parallel absent -> not this card
        overlap = len(qtokens & ttokens)
        extra = len(ttokens - qtokens)  # tokens the card has but query doesn't
        key = (overlap, -extra)
        if key > best_key:
            best_key, best = key, p
    return best if best and best_key[0] >= needed else None


def parse_pricecharting_json(data: dict, *, graded: bool = False) -> list[SoldComp]:
    if not data or data.get("status") == "error":
        return []
    name = data.get("product-name") or ""
    console = data.get("console-name") or ""
    title = f"{console} {name}".strip()
    if not title:
        return []
    search_url = _base() + "/search-products?q=" + quote_plus(title)

    field, grade = ("manual-only-price", "PSA 10") if graded else ("loose-price", "Ungraded")
    price = _dollars(data.get(field))
    if price is None:
        return []
    return [
        SoldComp(
            title=title,
            sold_price=price,
            sold_date=None,  # aggregate market price, not a single dated sale
            condition_grade=grade,
            listing_url=search_url,
            thumbnail_url=None,
            source="sportscardspro",
            marketplace="eBay",  # SportsCardsPro derives its prices from eBay
            kind="sold",
        )
    ]


def parse_grade_tiers(data: dict) -> list[SoldComp]:
    """Emit one informational comp per GRADED price tier present (PSA 9/10, BGS,
    SGC, CGC). Ungraded is excluded — `fetch_comps` already supplies it as the
    estimate-driving comp, and these tiers are deliberately tagged so the matcher
    files them as 'graded' (visible reference points, not raw-price inputs)."""
    if not data or data.get("status") == "error":
        return []
    name = data.get("product-name") or ""
    console = data.get("console-name") or ""
    title = f"{console} {name}".strip()
    if not title:
        return []
    search_url = _base() + "/search-products?q=" + quote_plus(title)

    comps: list[SoldComp] = []
    for field, grade, ungraded in _TIERS:
        if ungraded:
            continue
        price = _dollars(data.get(field))
        if price is None:
            continue
        comps.append(
            SoldComp(
                title=f"{title} [{grade}]",
                sold_price=price,
                sold_date=None,  # aggregate market price, not a dated sale
                condition_grade=grade,
                listing_url=search_url,
                source="sportscardspro",
                marketplace="eBay",
                kind="sold",
            )
        )
    return comps


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def product_page_url(data: dict) -> str | None:
    """Best-effort product web-page URL (the page that carries the recent-sales
    table). The API detail JSON doesn't return the URL, so we build it from the
    console/product slugs. Verify the shape with the verify_sportscardspro tool."""
    name, console = data.get("product-name"), data.get("console-name")
    if not (name and console):
        return None
    return f"{_base()}/game/{_slug(console)}/{_slug(name)}"


def parse_sold_date(text: str | None) -> str | None:
    if not text:
        return None
    iso = _ISO_DATE_RE.search(text)
    if iso:
        try:
            return date(int(iso[1]), int(iso[2]), int(iso[3])).isoformat()
        except ValueError:
            return None
    m = _DATE_RE.search(text)
    if not m:
        return None
    month = _MONTHS.get(m[1].title())
    if not month:
        return None
    try:
        return date(int(m[3]), month, int(m[2])).isoformat()
    except ValueError:
        return None


def parse_sales_table_html(html: str, *, page_url: str | None = None) -> list[SoldComp]:
    """Parse the product page's recent-sales table into INDIVIDUAL dated sales.

    Permissive by design (no official API): we take any table row carrying a
    price, pulling a date, title and grade where present. A markup change degrades
    to [] rather than throwing, so we never invent a sale.
    """
    tree = HTMLParser(html)
    comps: list[SoldComp] = []
    seen: set[tuple] = set()
    for row in tree.css("table tr, .sales tr, .price-data tr"):
        text = row.text(separator=" ", strip=True)
        price = _parse_price_text(text)
        if price is None:
            continue  # header / chrome rows have no price
        link = row.css_first("a[href]")
        href = urljoin(page_url, link.attributes.get("href")) if (link and page_url) else (
            link.attributes.get("href") if link else None
        )
        title_node = row.css_first(".title, .console, td a")
        title = (title_node.text(strip=True) if title_node else "") or "SportsCardsPro sale"
        sold_date = parse_sold_date(text)
        grade_m = _GRADE_RE.search(text)
        key = (title, price, sold_date)
        if key in seen:
            continue
        seen.add(key)
        comps.append(
            SoldComp(
                title=title,
                sold_price=price,
                sold_date=sold_date,
                condition_grade=grade_m.group(0).upper() if grade_m else None,
                listing_url=href,
                source="sportscardspro (sold)",
                marketplace="eBay",  # the recent-sales table is eBay completed sales
                kind="sold",
            )
        )
    return comps


def _parse_price_text(text: str | None) -> float | None:
    if not text:
        return None
    m = _PRICE_RE.search(text.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _lookup_detail(
    query: str, *, require_parallel: str | None = None, require_number: str | None = None
) -> dict | None:
    """Shared two-step lookup: search -> confident product -> detail JSON."""
    if not has_token():
        return None
    token = get_settings().pricecharting_token
    base = _base()
    try:
        listing = httpx.get(f"{base}/api/products", params={"t": token, "q": query}, timeout=30)
        listing.raise_for_status()
        products = listing.json().get("products", [])
    except Exception:  # noqa: BLE001
        logger.exception("Card-price product search failed for %r", query)
        return None

    best = select_best_product(
        products, query, require_parallel=require_parallel, require_number=require_number
    )
    if not best or not best.get("id"):
        logger.info("Card-price: no confident match for %r", query)
        return None

    try:
        detail = httpx.get(f"{base}/api/product", params={"t": token, "id": best["id"]}, timeout=30)
        detail.raise_for_status()
    except Exception:  # noqa: BLE001
        logger.exception("Card-price product detail failed for id %s", best.get("id"))
        return None
    return detail.json()


def fetch_comps(
    query: str,
    *,
    graded: bool = False,
    require_parallel: str | None = None,
    require_number: str | None = None,
) -> list[SoldComp]:
    detail = _lookup_detail(query, require_parallel=require_parallel, require_number=require_number)
    if detail is None:
        return []
    return parse_pricecharting_json(detail, graded=graded)


def fetch_grade_tiers(
    query: str, *, require_parallel: str | None = None, require_number: str | None = None
) -> list[SoldComp]:
    """Full graded-tier price breakdown for a card (informational comps)."""
    detail = _lookup_detail(query, require_parallel=require_parallel, require_number=require_number)
    if detail is None:
        return []
    return parse_grade_tiers(detail)


def parse_cover_image(tree: HTMLParser, page_url: str | None = None) -> str | None:
    """The card's cover scan from a product page. SportsCardsPro serves it from a
    public Google Storage bucket (images.pricecharting.com/<token>/240.jpg); we
    upgrade to the large /1600.jpg. Returns None for placeholder/chrome images."""
    node = tree.css_first(".cover img") or tree.css_first('img[itemprop="image"]')
    src = node.attributes.get("src") if node else None
    if not src or "lock" in src or "logo" in src:
        return None
    src = re.sub(r"/\d+\.jpg($|\?)", r"/1600.jpg\1", src)  # 240.jpg -> 1600.jpg
    return urljoin(page_url, src) if page_url else src


def fetch_product_image(
    query: str, *, require_parallel: str | None = None, require_number: str | None = None
) -> str | None:
    """The card's cover image from its SportsCardsPro product page (a clean
    catalogue scan of the exact card). Returns an absolute URL or None."""
    if not has_token():
        return None
    detail = _lookup_detail(query, require_parallel=require_parallel, require_number=require_number)
    if detail is None:
        return None
    url = product_page_url(detail)
    if not url:
        return None
    html = _get_product_page(url)
    if html is None:
        return None
    return parse_cover_image(HTMLParser(html), page_url=url)


def fetch_individual_sales(
    query: str, *, require_parallel: str | None = None, require_number: str | None = None
) -> list[SoldComp]:
    """Scrape the product page's recent-sales table for INDIVIDUAL dated sales.

    Off unless SPORTSCARDSPRO_SALES_ENABLED — this scrapes the web page (no API),
    same ToS-gray footing as the eBay/130point scrapers. Returns [] on any miss.
    """
    if not get_settings().sportscardspro_sales_enabled:
        return []
    detail = _lookup_detail(query, require_parallel=require_parallel, require_number=require_number)
    if detail is None:
        return []
    url = product_page_url(detail)
    if not url:
        return []
    html = _get_product_page(url)
    if html is None:
        return []
    # Scope to the "Time Warp" completed-sales tables when present (avoids the
    # price-summary / attributes tables), then keep only rows with a real date —
    # those are the individual historical sales we want.
    tree = HTMLParser(html)
    sales_html = "".join(tbl.html or "" for tbl in tree.css("table.hoverable-rows"))
    comps = [
        c for c in parse_sales_table_html(sales_html or html, page_url=url)
        if c.sold_date
    ]
    if not comps:
        logger.warning("SportsCardsPro: 0 parseable sales for %r (%s)", query, url)
    return comps
