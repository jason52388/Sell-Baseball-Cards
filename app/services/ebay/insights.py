"""eBay Buy **Marketplace Insights** API — real SOLD prices (last 90 days).

This is the only official source of eBay sold data. Access is gated: your eBay
app must be granted the `buy.marketplace.insights` scope. Until then this returns
no data and the caller reports "sold data pending" — never a fabricated price.

Enable via EBAY_INSIGHTS_ENABLED=true once eBay approves your application.
"""
from __future__ import annotations

import logging

import httpx

from app.config import get_settings
from app.services.ebay.base import SoldComp
from app.services.ebay.oauth import INSIGHTS_SCOPE, get_app_access_token
from app.services.ebay.scrape import _detect_grade

logger = logging.getLogger("ebay.insights")

INSIGHTS_URL = (
    "https://api.ebay.com/buy/marketplace_insights/v1_beta/item_sales/search"
)


class InsightsAccessError(RuntimeError):
    """Raised when Insights is enabled but the app lacks approved access."""


def is_enabled() -> bool:
    s = get_settings()
    return bool(s.ebay_insights_enabled and s.ebay_client_id and s.ebay_client_secret)


def parse_insights_json(data: dict) -> list[SoldComp]:
    comps: list[SoldComp] = []
    for it in data.get("itemSales", []):
        price = (it.get("lastSoldPrice") or {}).get("value")
        try:
            price = float(price) if price is not None else None
        except (TypeError, ValueError):
            price = None
        image = (it.get("image") or {}).get("imageUrl")
        sold_date = it.get("lastSoldDate")  # ISO-8601 timestamp
        if sold_date and "T" in sold_date:
            sold_date = sold_date.split("T", 1)[0]
        title = it.get("title")
        comps.append(
            SoldComp(
                title=title,
                sold_price=price,
                sold_date=sold_date,
                condition_grade=_detect_grade(title) or it.get("condition"),
                listing_url=it.get("itemWebUrl"),
                thumbnail_url=image,
                source="ebay (sold)",
                kind="sold",
            )
        )
    return comps


def fetch_sold_comps(query: str, *, graded: bool = False) -> list[SoldComp]:
    if not is_enabled():
        return []
    q = f"{query} PSA 10" if graded else query
    s = get_settings()
    try:
        token = get_app_access_token(live=True, scope=INSIGHTS_SCOPE)
        resp = httpx.get(
            INSIGHTS_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": s.ebay_marketplace_id,
            },
            params={"q": q, "limit": "50"},
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Insights request error for %r", q)
        raise InsightsAccessError(str(exc)) from exc

    if resp.status_code in (401, 403):
        raise InsightsAccessError(
            "Marketplace Insights access not approved for this eBay app yet."
        )
    resp.raise_for_status()
    return parse_insights_json(resp.json())
