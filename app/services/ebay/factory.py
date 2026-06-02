"""Select a listing client based on EBAY_MODE (preview | sandbox | live)."""
from __future__ import annotations

from app.config import get_settings
from app.services.ebay.base import ListingClient


def get_listing_client() -> ListingClient:
    mode = get_settings().ebay_mode.lower()
    if mode in ("sandbox", "live"):
        from app.services.ebay.sandbox import SandboxEbayClient

        return SandboxEbayClient(live=mode == "live")
    from app.services.ebay.preview import PreviewListingClient

    return PreviewListingClient()
