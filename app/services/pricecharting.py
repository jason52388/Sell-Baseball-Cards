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

Sports-card price fields (in pennies):
  loose-price        -> Ungraded
  manual-only-price  -> PSA 10
Each maps to a single SoldComp tagged source="sportscardspro", kind="sold".
"""
from __future__ import annotations

import logging
import re
from urllib.parse import quote_plus

import httpx

from app.config import get_settings
from app.services.ebay.base import SoldComp

logger = logging.getLogger("pricecharting")


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


def select_best_product(products: list[dict], query: str) -> dict | None:
    """Pick the product that genuinely matches the query, or None.

    Requires the query's year (if any) to appear in the candidate and a
    reasonable token overlap — so unrelated products are rejected.
    """
    qtokens = _tokens(query)
    if not qtokens:
        return None
    years = {t for t in qtokens if len(t) == 4 and t.isdigit()}
    needed = max(3, (len(qtokens) + 1) // 2)

    best: dict | None = None
    best_key = (0, 0)  # (overlap, -extra_tokens) — higher overlap, fewer extras
    for p in products:
        ttokens = _tokens(_product_title(p))
        if years and not (years & ttokens):
            continue  # wrong/!missing year -> not this card
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
            kind="sold",
        )
    ]


def fetch_comps(query: str, *, graded: bool = False) -> list[SoldComp]:
    if not has_token():
        return []
    token = get_settings().pricecharting_token
    base = _base()
    try:
        listing = httpx.get(f"{base}/api/products", params={"t": token, "q": query}, timeout=30)
        listing.raise_for_status()
        products = listing.json().get("products", [])
    except Exception:  # noqa: BLE001
        logger.exception("Card-price product search failed for %r", query)
        return []

    best = select_best_product(products, query)
    if not best or not best.get("id"):
        logger.info("Card-price: no confident match for %r", query)
        return []

    try:
        detail = httpx.get(f"{base}/api/product", params={"t": token, "id": best["id"]}, timeout=30)
        detail.raise_for_status()
    except Exception:  # noqa: BLE001
        logger.exception("Card-price product detail failed for id %s", best.get("id"))
        return []
    return parse_pricecharting_json(detail.json(), graded=graded)
