"""eBay Buy **Browse** API provider — real CURRENT ASKING prices (not sold).

Uses the client-credentials (application) token. Returns active listings as
SoldComp objects tagged kind="active" so the rest of the app can show them as
"current asking" prices and never confuse them with completed sales.
"""
from __future__ import annotations

import logging

import httpx

from app.config import get_settings
from app.services.ebay.base import SoldComp
from app.services.ebay.oauth import get_app_access_token
from app.services.ebay.scrape import _detect_grade

logger = logging.getLogger("ebay.browse")

BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"


def has_credentials() -> bool:
    s = get_settings()
    return bool(s.ebay_client_id and s.ebay_client_secret)


def parse_browse_json(data: dict) -> list[SoldComp]:
    comps: list[SoldComp] = []
    for it in data.get("itemSummaries", []):
        price = (it.get("price") or {}).get("value")
        try:
            price = float(price) if price is not None else None
        except (TypeError, ValueError):
            price = None
        image = (it.get("image") or {}).get("imageUrl")
        if not image and it.get("thumbnailImages"):
            image = it["thumbnailImages"][0].get("imageUrl")
        title = it.get("title")
        comps.append(
            SoldComp(
                title=title,
                sold_price=price,
                sold_date=None,  # active listing — not a sale
                condition_grade=_detect_grade(title) or it.get("condition"),
                listing_url=it.get("itemWebUrl"),
                thumbnail_url=image,
                source="ebay (active)",
                kind="active",
            )
        )
    return comps


def fetch_active_comps(query: str, *, graded: bool = False) -> list[SoldComp]:
    if not has_credentials():
        return []
    q = f"{query} PSA 10" if graded else query
    s = get_settings()
    try:
        token = get_app_access_token(live=True)
        resp = httpx.get(
            BROWSE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": s.ebay_marketplace_id,
            },
            params={"q": q, "limit": "50", "filter": "buyingOptions:{FIXED_PRICE}"},
            timeout=30,
        )
        resp.raise_for_status()
    except Exception:  # noqa: BLE001
        logger.exception("eBay Browse search failed for %r", q)
        return []
    return parse_browse_json(resp.json())
