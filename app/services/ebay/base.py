"""eBay listing client interface + shared data types.

`create_listing` creates a fixed-price (Buy-It-Now) listing. Implementations:
`preview` (no creds — builds the payload but publishes nothing) and
`sandbox`/`live` (real Sell API). Pricing/comps live separately in
`app.services.comp_sources` and always use real sold data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class SoldComp:
    title: str
    sold_price: float | None
    sold_date: str | None = None
    condition_grade: str | None = None
    listing_url: str | None = None
    thumbnail_url: str | None = None
    source: str = "ebay"
    # "sold" = a completed sale; "active" = a current asking price (Browse API).
    kind: str = "sold"


@dataclass
class ListingResult:
    sku: str
    offer_id: str | None
    listing_id: str | None
    status: str  # published | preview | failed
    list_price: float
    response: dict = field(default_factory=dict)
    message: str | None = None


class ListingClient(Protocol):
    def create_listing(self, card, list_price: float) -> ListingResult:
        ...
