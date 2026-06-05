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
    # `source` is the PROVIDER the data came through (e.g. "130point (sold)",
    # "sportscardspro", "ebay (sold)"). `marketplace` is the ORIGINAL venue the
    # sale happened on (e.g. "eBay", "PWCC", "Goldin") — providers like 130point
    # and SportsCardsPro aggregate sales from eBay and others, so we keep both.
    source: str = "ebay"
    marketplace: str | None = None
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

    def create_set_listing(self, cards: list, list_price: float) -> ListingResult:
        """Combine multiple cards into ONE lot listing (all cards + all photos)."""
        ...
