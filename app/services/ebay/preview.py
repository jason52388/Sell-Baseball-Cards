"""Preview listing client: needs no credentials and publishes NOTHING.

It builds the payload with the SAME builder the real Sell API client uses
(listing_common.build_single_payload / build_lot_payload), so the preview is
exactly what a publish would send. The one difference: a real publish uploads
each photo to eBay Picture Services and sends those URLs, while the preview
shows the public crop URLs (it never uploads anything).
"""
from __future__ import annotations

import logging

from app.config import get_settings
from app.services.ebay.base import ListingResult
from app.services.ebay.listing_common import (
    build_lot_payload,
    build_single_payload,
    card_image_urls,
    public_crop_url,
    set_image_paths,
)

logger = logging.getLogger("ebay.preview")


class PreviewListingClient:
    def create_listing(self, card, list_price: float) -> ListingResult:
        s = get_settings()
        images = card_image_urls(
            card, s.public_image_base_url, include_reference=s.ebay_include_reference_image
        )
        payload = build_single_payload(card, list_price, s, images)
        logger.info("[PREVIEW] would publish eBay listing: %s", payload)
        return ListingResult(
            sku=payload["sku"],
            offer_id=None,
            listing_id=None,
            status="preview",
            list_price=list_price,
            response={"preview": True, "payload": payload},
            message="PREVIEW only: nothing was listed. Set EBAY_MODE=sandbox or "
            "live with credentials to publish for real.",
        )

    def create_set_listing(self, cards: list, list_price: float) -> ListingResult:
        s = get_settings()
        images = [
            u for u in (public_crop_url(p, s.public_image_base_url) for p in set_image_paths(cards))
            if u
        ]
        payload = build_lot_payload(cards, list_price, s, images)
        logger.info("[PREVIEW] would publish eBay SET listing of %d cards", len(cards))
        return ListingResult(
            sku=payload["sku"],
            offer_id=None,
            listing_id=None,
            status="preview",
            list_price=list_price,
            response={"preview": True, "payload": payload},
            message=f"PREVIEW only: {len(cards)} cards would list as one lot "
            f"({len(images)} photo(s)). Set EBAY_MODE=sandbox/live to publish.",
        )
