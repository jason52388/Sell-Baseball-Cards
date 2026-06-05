"""Preview listing client — needs no credentials and publishes NOTHING.

It builds the exact payload that the real Sell API call would send and returns a
result with status="preview" so the UI can show the user precisely what would be
listed. It never returns "published" and never contacts eBay. Use this to try
the "List on eBay" flow before configuring real credentials.
"""
from __future__ import annotations

import logging

from app.config import get_settings
from app.services.ebay.base import ListingResult
from app.services.ebay.listing_common import (
    build_aspects,
    build_set_aspects,
    build_set_description,
    build_set_title,
    build_title,
    map_condition,
    set_image_urls,
    set_sku,
)

logger = logging.getLogger("ebay.preview")


class PreviewListingClient:
    def create_listing(self, card, list_price: float) -> ListingResult:
        s = get_settings()
        sku = f"CARD-{card.id}"
        payload = {
            "inventory_item": {
                "sku": sku,
                "product": {
                    "title": build_title(card),
                    "aspects": build_aspects(card),
                },
                "condition": map_condition(card, s.ebay_condition),
                "availability": {"shipToLocationAvailability": {"quantity": 1}},
            },
            "offer": {
                "sku": sku,
                "marketplaceId": s.ebay_marketplace_id,
                "format": "FIXED_PRICE",
                "categoryId": s.ebay_category_id,
                "pricingSummary": {"price": {"value": str(list_price), "currency": "USD"}},
            },
        }
        logger.info("[PREVIEW] would publish eBay listing: %s", payload)
        return ListingResult(
            sku=sku,
            offer_id=None,
            listing_id=None,
            status="preview",
            list_price=list_price,
            response={"preview": True, "payload": payload},
            message="PREVIEW only — nothing was listed. Set EBAY_MODE=sandbox or "
            "live with credentials to publish for real.",
        )

    def create_set_listing(self, cards: list, list_price: float) -> ListingResult:
        s = get_settings()
        sku = set_sku(cards)
        images = set_image_urls(cards, s.public_image_base_url)
        product = {
            "title": build_set_title(cards),
            "aspects": build_set_aspects(cards),
        }
        if images:
            product["imageUrls"] = images
        payload = {
            "inventory_item": {
                "sku": sku,
                "product": product,
                "condition": s.ebay_condition,
                "availability": {"shipToLocationAvailability": {"quantity": 1}},
            },
            "offer": {
                "sku": sku,
                "marketplaceId": s.ebay_marketplace_id,
                "format": "FIXED_PRICE",
                "categoryId": s.ebay_lot_category_id,
                "listingDescription": build_set_description(cards, shown_images=len(images)),
                "pricingSummary": {"price": {"value": str(list_price), "currency": "USD"}},
            },
        }
        logger.info("[PREVIEW] would publish eBay SET listing of %d cards", len(cards))
        return ListingResult(
            sku=sku,
            offer_id=None,
            listing_id=None,
            status="preview",
            list_price=list_price,
            response={"preview": True, "payload": payload},
            message=f"PREVIEW only — {len(cards)} cards would list as one lot "
            f"({len(images)} photo(s)). Set EBAY_MODE=sandbox/live to publish.",
        )
