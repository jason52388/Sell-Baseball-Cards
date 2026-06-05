"""Aggregate real price comps from every configured source. No fabricated data.

SOLD prices:
  - eBay Marketplace Insights API   (insights.py)        — official, gated
  - PriceCharting API               (pricecharting.py)   — paid token
  - Headless-browser eBay scrape    (browser_scrape.py)  — best-effort, ToS-gray
ACTIVE asking prices:
  - eBay Browse API                 (browse.py)          — free keyset

`gather_comps` returns (comps, notes). `notes` carry honest, user-facing
explanations so the UI never guesses and a price is never invented.
"""
from __future__ import annotations

import logging

from app.config import get_settings
from app.services import comp_cache, pricecharting
from app.services.ebay import browse, browser_scrape, insights
from app.services.ebay.base import SoldComp
from app.services.ebay.scrape import fetch_sold_comps as scrape_sold

logger = logging.getLogger("comp_sources")


def gather_comps(
    query: str, *, graded: bool = False, use_cache: bool = True
) -> tuple[list[SoldComp], list[str]]:
    s = get_settings()

    # Persistent cache: reuse a recent pooled result for this card identity
    # instead of re-hitting the price APIs (see settings.price_cache_ttl_days).
    if use_cache:
        cached = comp_cache.get(query, graded=graded, marketplace=s.ebay_marketplace_id)
        if cached is not None:
            return cached, ["prices reused from cache (no API calls)"]

    comps: list[SoldComp] = []
    notes: list[str] = []
    has_ebay_creds = bool(s.ebay_client_id and s.ebay_client_secret)

    # --- SOLD: eBay Marketplace Insights ---
    if insights.is_enabled():
        try:
            comps.extend(insights.fetch_sold_comps(query, graded=graded))
        except insights.InsightsAccessError as exc:
            notes.append(f"eBay sold (Insights) unavailable: {exc}")
    elif has_ebay_creds:
        notes.append(
            "eBay sold (Insights) off: set EBAY_INSIGHTS_ENABLED=true once approved."
        )

    # --- SOLD: PriceCharting ---
    if pricecharting.has_token():
        comps.extend(pricecharting.fetch_comps(query, graded=graded))

    # --- SOLD: headless-browser eBay scrape (best-effort) ---
    if browser_scrape.is_enabled():
        comps.extend(browser_scrape.fetch_sold_comps(query, graded=graded))

    # --- ACTIVE: eBay Browse ---
    if has_ebay_creds:
        comps.extend(browse.fetch_active_comps(query, graded=graded))

    if not comps:
        # Last resort: plain scrape (usually 403). Keeps behavior graceful.
        scraped = scrape_sold(query)
        comps.extend(scraped)
        if not scraped and not (has_ebay_creds or pricecharting.has_token()):
            notes.append(
                "No price source configured. Add EBAY_CLIENT_ID/SECRET, a "
                "PRICECHARTING_TOKEN, or enable EBAY_BROWSER_SCRAPE_ENABLED."
            )

    # Only cache real results — never lock in an empty result (the card may get
    # comps later, e.g. once Insights is enabled or a new listing appears).
    if use_cache and comps:
        comp_cache.put(query, graded=graded, marketplace=s.ebay_marketplace_id, comps=comps)

    return comps, notes


def live_comps(query: str, *, graded: bool = False) -> list[SoldComp]:
    """Back-compat helper returning just the comps."""
    return gather_comps(query, graded=graded)[0]
