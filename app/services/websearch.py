"""Web-search price fallback. Stubbed by default.

Returns SoldComp-like price points sourced from the web when eBay sold comps
are unavailable. Real implementation would call a search API using
settings.websearch_api_key; until configured it returns no results so the
safeguard correctly flags the card for manual review.
"""
from __future__ import annotations

from app.config import get_settings
from app.services.ebay.base import SoldComp


def get_web_price_points(query: str) -> list[SoldComp]:
    settings = get_settings()
    if not settings.websearch_api_key:
        return []
    # Placeholder for a real web-search integration. Each price point should be
    # returned as a SoldComp with source="web" and a listing_url for audit.
    return []
