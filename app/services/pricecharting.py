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
from app.services.matching import (
    GRADE_RE,
    detect_grade,
    parallel_markers,
    player_last_name,
)

# Short-lived cache of fetched product-page HTML, keyed by URL. The image scrape
# and the sales-history scrape hit the SAME page, so this avoids fetching the
# (large) page twice for one card.
_PAGE_TTL = 600  # seconds
_PAGE_CACHE_MAX = 64  # pages; a full reprice would otherwise hold hundreds
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
        # Evict on write: entries are only checked for staleness on read, so
        # without this a large reprice keeps every product page it ever fetched
        # (hundreds of KB each) for the life of the process.
        for stale_url in [u for u, (ts, _) in _page_cache.items() if now - ts >= _PAGE_TTL]:
            del _page_cache[stale_url]
        if len(_page_cache) >= _PAGE_CACHE_MAX:
            _page_cache.clear()
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
# One grade pattern for every source (see matching.GRADE_RE).
_GRADE_RE = GRADE_RE
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
    require_player: str | None = None,
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
      - `require_player`: the candidate must carry the player's last name, so a
        same-year/same-number product for someone else (even another sport)
        can never price the card or supply its reference photo.
      - Parallel markers (Gold, Refractor, /50, SP, ...) in a candidate that the
        query does not name reject it: a base card must never be priced off its
        parallel. Markers that are part of the query (set "Topps Gold Label",
        parallel "Gold") are allowed. Bracketed variant names the query lacks
        ("[Global Impact]") are a tie-break penalty, not a rejection, because a
        subset is often catalogued that way.
    """
    qtokens = _tokens(query)
    if not qtokens:
        return None
    years = {t for t in qtokens if len(t) == 4 and t.isdigit()}
    needed = max(3, (len(qtokens) + 1) // 2)
    req_parallel = _required_parallel_tokens(require_parallel)
    number = str(require_number).lstrip("#").strip() if require_number else ""
    number_re = re.compile(rf"#?\b{re.escape(number)}\b") if number else None
    last_name = player_last_name(require_player)

    best: dict | None = None
    best_key = (0, 0, 0)  # (overlap, -bracket_extras, -extra_tokens)
    for p in products:
        title = _product_title(p)
        title_norm = title.lower()
        ttokens = _tokens(title)
        if years and not (years & ttokens):
            continue  # wrong/!missing year -> not this card
        if last_name and last_name not in ttokens:
            continue  # someone else's card
        if number_re is not None:
            if not number_re.search(title_norm):
                continue  # has a number but this candidate isn't it
        elif req_parallel and not req_parallel.issubset(ttokens):
            continue  # no number to disambiguate + parallel absent -> not this card
        if parallel_markers(title, qtokens, allow_serial=bool(require_parallel)):
            continue  # names a parallel the card does not have
        bracket = set()
        for inner in re.findall(r"\[([^\]]*)\]", title):
            bracket |= _tokens(inner)
        overlap = len(qtokens & ttokens)
        extra = len(ttokens - qtokens)  # tokens the card has but query doesn't
        key = (overlap, -len(bracket - qtokens), -extra)
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
    # Link straight to this card's product page, not a generic search.
    link = product_page_url(data) or (_base() + "/search-products?q=" + quote_plus(title))

    field, grade = ("manual-only-price", "PSA 10") if graded else ("loose-price", "Ungraded")
    price = _dollars(data.get(field))
    if price is None:
        return []
    return [
        SoldComp(
            title=title,
            sold_price=price,
            # Aggregate market price, not a single sale: dated with the fetch day
            # so the recency window applies to it (comp_cache keeps it as a
            # snapshot, never as accumulated sale history).
            sold_date=date.today().isoformat(),
            condition_grade=grade,
            listing_url=link,
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
    link = product_page_url(data) or (_base() + "/search-products?q=" + quote_plus(title))

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
                sold_date=date.today().isoformat(),  # fetch day (see above)
                condition_grade=grade,
                listing_url=link,
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


# The product page keeps one completed-sales table per grade tier, inside a
# container whose id is "completed-auctions-<tier>". "used" is the ungraded
# table; every other tier is a graded one.
_TIER_GRADES = {"manual-only": "PSA 10"}
_EBAY_TAG_RE = re.compile(r"\s*\[(?:ebay|e-bay)\]\s*", re.IGNORECASE)


def parse_sales_table_html(
    html: str,
    *,
    page_url: str | None = None,
    product_title: str | None = None,
    tier: str | None = None,
) -> list[SoldComp]:
    """Parse the product page's recent-sales table into INDIVIDUAL dated sales.

    Permissive by design (no official API): we take any table row carrying a
    price, pulling a date, title and grade where present. A markup change degrades
    to [] rather than throwing, so we never invent a sale.

    `product_title` is the matched SportsCardsPro product ("Baseball Cards 2001
    Topps Pedro Martinez #399"). Sellers' titles are often thin ("Pedro Martinez
    card"), so the product identity is attached to each row for matching, and
    used alone when a row has no title. `tier` is the grade-tier table the rows
    came from ("used" = ungraded); rows from any other tier are graded even if
    the seller's title does not say so.
    """
    tree = HTMLParser(html)
    comps: list[SoldComp] = []
    seen: set[tuple] = set()
    tier_graded = bool(tier) and tier != "used"
    for row in tree.css("table tr, .sales tr, .price-data tr"):
        text = row.text(separator=" ", strip=True)
        price_node = row.css_first(".js-price")
        price = _parse_price_text(price_node.text(strip=True) if price_node else None)
        if price is None:
            price = _parse_price_text(text)
        if price is None:
            continue  # header / chrome rows have no price
        link = row.css_first("a[href]")
        raw_href = link.attributes.get("href") if link else None
        href = urljoin(page_url, raw_href) if (raw_href and page_url) else raw_href
        # Premium-locked sales link to a generic upsell page, not the actual
        # listing — fall back to the card's own product page instead.
        if not href or "sportscardspro-premium" in href or "/account" in href:
            href = page_url
        title_node = row.css_first(".title, .console, td a")
        row_title = title_node.text(separator=" ", strip=True) if title_node else ""
        row_title = re.sub(r"\s+", " ", _EBAY_TAG_RE.sub(" ", row_title)).strip()
        if row_title and product_title:
            title = f"{row_title} (SportsCardsPro: {product_title})"
        else:
            title = row_title or product_title or "SportsCardsPro sale"
        sold_date = parse_sold_date(text)
        grade = detect_grade(row_title or text)
        if grade is None and tier_graded:
            grade = _TIER_GRADES.get(tier, "Graded")
        key = (title, price, sold_date)
        if key in seen:
            continue
        seen.add(key)
        comps.append(
            SoldComp(
                title=title,
                sold_price=price,
                sold_date=sold_date,
                condition_grade=grade,
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


class PriceChartingError(RuntimeError):
    """The catalogue could not answer (network error, rate limit, server error).

    Distinct from "no match" (which returns nothing): a failure is reported as a
    source status so the card says WHY it has no price. `state` is the status
    code comp_sources records.
    """

    state = "error"

    def __init__(self, message: str, *, state: str | None = None):
        super().__init__(message)
        if state:
            self.state = state


class PriceChartingAuthError(PriceChartingError):
    """The API token was rejected (expired, unknown, or out of subscription).

    Distinct from "no match": a rejected token means every card silently loses
    its sold-price source, which must be reported rather than swallowed.
    """

    state = "auth_expired"


# The token travels as a `t=` query parameter, so it lands in any URL that ends
# up in an exception message, a log line or a traceback.
_TOKEN_PARAM_RE = re.compile(r"([?&]t=)[^&\s\"']+")

# Statuses the catalogue uses to reject a token: 410 "Access token has expired",
# 403 "Unknown access token", 401 for good measure.
_AUTH_STATUSES = {401, 403, 410}


def redact_token(text: str) -> str:
    """Remove the API token from anything that may be logged or displayed."""
    if not text:
        return text
    token = get_settings().pricecharting_token
    if token:
        text = text.replace(token, "***")
    return _TOKEN_PARAM_RE.sub(r"\1***", text)


def _check_auth(resp: httpx.Response) -> None:
    """Turn a token rejection into a clear, token-free error."""
    if resp.status_code not in _AUTH_STATUSES:
        return
    detail = ""
    try:
        detail = (resp.json() or {}).get("error-message") or ""
    except Exception:  # noqa: BLE001 — non-JSON body
        pass
    detail = redact_token(detail) or f"HTTP {resp.status_code}"
    raise PriceChartingAuthError(
        f"SportsCardsPro rejected the API token: {detail}. Sold prices are "
        f"unavailable until PRICECHARTING_TOKEN is renewed (then restart the app)."
    )


def _failure(what: str, exc: Exception) -> PriceChartingError:
    """A token-free, status-coded error for a failed catalogue call."""
    status = None
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
    msg = f"SportsCardsPro {what} failed: " + (
        f"HTTP {status}" if status else redact_token(str(exc)) or type(exc).__name__
    )
    return PriceChartingError(msg, state="quota" if status == 429 else "error")


def _lookup_detail(
    query: str,
    *,
    require_parallel: str | None = None,
    require_number: str | None = None,
    require_player: str | None = None,
) -> dict | None:
    """Shared two-step lookup: search -> confident product -> detail JSON.

    Returns None for an ordinary miss (no confident product). Raises
    PriceChartingAuthError if the token is rejected and PriceChartingError for a
    transient failure, so the caller can report the source as failing instead
    of mistaking it for "no match".
    """
    if not has_token():
        return None
    token = get_settings().pricecharting_token
    base = _base()
    try:
        listing = httpx.get(f"{base}/api/products", params={"t": token, "q": query}, timeout=30)
        _check_auth(listing)
        listing.raise_for_status()
        products = listing.json().get("products", [])
    except PriceChartingError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Card-price product search failed for %r: %s", query, redact_token(str(exc))
        )
        raise _failure("product search", exc) from None

    best = select_best_product(
        products, query, require_parallel=require_parallel, require_number=require_number,
        require_player=require_player,
    )
    if not best or not best.get("id"):
        logger.info("Card-price: no confident match for %r", query)
        return None

    try:
        detail = httpx.get(f"{base}/api/product", params={"t": token, "id": best["id"]}, timeout=30)
        _check_auth(detail)
        detail.raise_for_status()
        return detail.json()
    except PriceChartingError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Card-price product detail failed for id %s: %s",
            best.get("id"), redact_token(str(exc)),
        )
        raise _failure("product detail", exc) from None


def fetch_comps(
    query: str,
    *,
    graded: bool = False,
    require_parallel: str | None = None,
    require_number: str | None = None,
    require_player: str | None = None,
) -> list[SoldComp]:
    detail = _lookup_detail(
        query, require_parallel=require_parallel, require_number=require_number,
        require_player=require_player,
    )
    if detail is None:
        return []
    return parse_pricecharting_json(detail, graded=graded)


def fetch_grade_tiers(
    query: str,
    *,
    require_parallel: str | None = None,
    require_number: str | None = None,
    require_player: str | None = None,
) -> list[SoldComp]:
    """Full graded-tier price breakdown for a card (informational comps)."""
    detail = _lookup_detail(
        query, require_parallel=require_parallel, require_number=require_number,
        require_player=require_player,
    )
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
    query: str,
    *,
    require_parallel: str | None = None,
    require_number: str | None = None,
    require_player: str | None = None,
) -> str | None:
    """The card's cover image from its SportsCardsPro product page (a clean
    catalogue scan of the exact card). Returns an absolute URL or None."""
    if not has_token():
        return None
    detail = _lookup_detail(
        query, require_parallel=require_parallel, require_number=require_number,
        require_player=require_player,
    )
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
    query: str,
    *,
    require_parallel: str | None = None,
    require_number: str | None = None,
    require_player: str | None = None,
) -> list[SoldComp]:
    """Scrape the product page's recent-sales table for INDIVIDUAL dated sales.

    Off unless SPORTSCARDSPRO_SALES_ENABLED — this scrapes the web page (no API),
    same ToS-gray footing as the eBay/130point scrapers. Returns [] on any miss.
    """
    if not get_settings().sportscardspro_sales_enabled:
        return []
    detail = _lookup_detail(
        query, require_parallel=require_parallel, require_number=require_number,
        require_player=require_player,
    )
    if detail is None:
        return []
    url = product_page_url(detail)
    if not url:
        return []
    html = _get_product_page(url)
    if html is None:
        raise PriceChartingError(
            "SportsCardsPro product page for recent sales could not be loaded "
            "(blocked or offline; see the server log)",
            state="blocked",
        )
    comps = _dated_sales_from_html(html, url, product_title=_product_title(detail))
    if not comps:
        logger.warning("SportsCardsPro: 0 parseable sales for %r (%s)", query, url)
    return comps


def _dated_sales_from_html(
    html: str, url: str | None, *, product_title: str | None = None
) -> list[SoldComp]:
    """Individual dated completed sales from a product page's "Time Warp" tables.
    Scopes to those tables (avoids the price-summary/attributes tables), tags
    each row with the grade tier of the table it sits in, and keeps only rows
    with a real date."""
    tree = HTMLParser(html)
    comps: list[SoldComp] = []
    tiered = tree.css('[id^="completed-auctions-"]')
    if tiered:
        for box in tiered:
            tier = (box.attributes.get("id") or "")[len("completed-auctions-"):]
            comps += parse_sales_table_html(
                box.html or "", page_url=url, product_title=product_title, tier=tier
            )
    else:
        sales_html = "".join(tbl.html or "" for tbl in tree.css("table.hoverable-rows"))
        comps = parse_sales_table_html(
            sales_html or html, page_url=url, product_title=product_title
        )
    return [c for c in comps if c.sold_date]


# --- Pricing from a user-pasted SportsCardsPro product URL ---------------------

_PID_RE = re.compile(r"/offers\?product=(\d+)")
_PAGE_PRICE_RE = re.compile(r"\$([\d,]+\.\d{2})")


def is_scp_url(url: str) -> bool:
    u = (url or "").strip().lower()
    return u.startswith("http") and ("sportscardspro.com" in u or "pricecharting.com" in u)


def _extract_product_id(html: str) -> str | None:
    m = _PID_RE.search(html or "")
    return m.group(1) if m else None


def _fetch_detail_by_id(pid: str) -> dict | None:
    if not has_token():
        return None
    try:
        r = httpx.get(
            f"{_base()}/api/product",
            params={"t": get_settings().pricecharting_token, "id": pid},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()
    except Exception:  # noqa: BLE001
        logger.warning("SportsCardsPro detail-by-id failed for %s", pid)
        return None


def ident_from_detail(detail: dict) -> dict:
    """Pull a card identity (player/year/set/number) out of a product detail so a
    user-pasted URL can correct a mis-identified card."""
    console = (detail.get("console-name") or "").strip()  # "Baseball Cards 2001 Topps"
    name = (detail.get("product-name") or "").strip()      # "Barry Bonds #497"
    ym = re.search(r"\b(19|20)\d{2}\b", console)
    year = ym.group(0) if ym else None
    set_brand = console
    set_brand = re.sub(r"(?i)\bbaseball cards\b", "", set_brand)
    if year:
        set_brand = set_brand.replace(year, "")
    set_brand = re.sub(r"\s+", " ", set_brand).strip() or None
    num_m = re.search(r"#\s*([A-Za-z0-9-]+)", name)
    number = num_m.group(1) if num_m else None
    player = re.sub(r"#.*$", "", name)
    player = re.sub(r"\[[^\]]*\]", "", player)  # drop "[Refractor]" etc.
    player = re.sub(r"\s+", " ", player).strip() or None
    return {"player": player, "year": year, "set_brand": set_brand, "card_number": number}


def data_from_url(url: str, *, graded: bool = False) -> tuple[list[SoldComp], list[SoldComp], str | None, dict | None]:
    """Scrape a pasted SportsCardsPro product URL. Returns
    (raw_comps, graded_tier_comps, cover_image_url, identity) — for when the
    automatic search matched the wrong card or nothing."""
    html = _get_product_page(url)
    if html is None:
        return [], [], None, None
    tree = HTMLParser(html)
    pid = _extract_product_id(html)
    detail = _fetch_detail_by_id(pid) if pid else None

    raw: list[SoldComp] = []
    graded_tiers: list[SoldComp] = []
    ident: dict | None = None
    if detail and detail.get("status") != "error":
        raw = parse_pricecharting_json(detail, graded=graded)
        if not graded:
            graded_tiers = parse_grade_tiers(detail)
        ident = ident_from_detail(detail)
    else:
        raw = _page_price_comps(tree, url)  # fallback: scrape the price off the page

    if get_settings().sportscardspro_sales_enabled:
        h1 = tree.css_first("h1")
        product_title = (
            _product_title(detail) if detail and detail.get("status") != "error"
            else (h1.text(separator=" ", strip=True) if h1 else None)
        )
        raw += _dated_sales_from_html(html, url, product_title=product_title)

    image = parse_cover_image(tree, page_url=url)
    return raw, graded_tiers, image, ident


def _page_price_comps(tree: HTMLParser, url: str) -> list[SoldComp]:
    """Fallback when the API id route is unavailable: read the Ungraded price
    straight off the product page."""
    node = tree.css_first("#used_price")
    if node is None:
        return []
    m = _PAGE_PRICE_RE.search(node.text(strip=True) or "")
    if not m:
        return []
    try:
        price = float(m.group(1).replace(",", ""))
    except ValueError:
        return []
    title = tree.css_first("h1")
    return [
        SoldComp(
            title=title.text(strip=True) if title else "SportsCardsPro",
            sold_price=price,
            sold_date=date.today().isoformat(),  # market price as of today
            condition_grade="Ungraded",
            listing_url=url,
            source="sportscardspro",
            marketplace="eBay",
            kind="sold",
        )
    ]
