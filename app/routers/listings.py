"""Listing endpoints: create eBay Buy-It-Now listings for priced cards.

- POST /api/listings/sell        — batch (list of card_ids)
- POST /api/cards/{id}/list      — single card ("List on eBay" button)

A listing is only attempted for cards in a sellable status. With EBAY_MODE in
its default `preview`, nothing is published — the result has status="preview"
and the card stays `priced`. Only a real `published` result marks it `listed`.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import (
    STATUS_LIST_FAILED,
    STATUS_LISTED,
    STATUS_PRICED,
    STATUS_SELECTED,
    Card,
    Listing,
)
from app.schemas import SellRequest, SellResponse, SellResult
from app.services.ebay.factory import get_listing_client

logger = logging.getLogger("listings")
router = APIRouter(tags=["listings"])

# Only these statuses may be listed — the hard safeguard against auto-listing
# anything low-confidence or under threshold.
SELLABLE = {STATUS_PRICED, STATUS_SELECTED}


def _list_one(card_id: int, db: Session, client, settings) -> SellResult:
    card = db.get(Card, card_id)
    if card is None:
        return SellResult(card_id=card_id, status="skipped", message="not found")
    if card.status not in SELLABLE:
        return SellResult(
            card_id=card_id, status="skipped",
            message=f"not sellable (status={card.status})",
        )
    if not card.estimated_price:
        return SellResult(card_id=card_id, status="skipped", message="no estimated price")

    list_price = round(card.estimated_price * settings.price_markup, 2)
    try:
        result = client.create_listing(card, list_price)
    except Exception as exc:  # noqa: BLE001
        logger.exception("listing failed for card %s", card_id)
        db.add(Listing(
            card_id=card_id, ebay_mode=settings.ebay_mode, list_price=list_price,
            status="failed", response_json=json.dumps({"error": str(exc)}),
        ))
        # A failed publish attempt does not corrupt the card's priced state.
        db.commit()
        return SellResult(card_id=card_id, status="failed", message=str(exc))

    db.add(Listing(
        card_id=card_id, ebay_mode=settings.ebay_mode, sku=result.sku,
        offer_id=result.offer_id, listing_id=result.listing_id,
        list_price=result.list_price, status=result.status,
        response_json=json.dumps(result.response),
    ))
    # Only a genuine publish moves the card to `listed`. Preview keeps it priced.
    if result.status == "published":
        card.status = STATUS_LISTED
    db.commit()
    return SellResult(
        card_id=card_id, status=result.status, listing_id=result.listing_id,
        list_price=result.list_price, message=result.message,
    )


@router.post("/api/listings/sell", response_model=SellResponse)
def sell(req: SellRequest, db: Session = Depends(get_db)) -> SellResponse:
    settings = get_settings()
    client = get_listing_client()
    return SellResponse(results=[_list_one(cid, db, client, settings) for cid in req.card_ids])


@router.post("/api/cards/{card_id}/list", response_model=SellResult)
def list_one(card_id: int, db: Session = Depends(get_db)) -> SellResult:
    """Create an eBay listing for a single card (the 'List on eBay' button)."""
    settings = get_settings()
    client = get_listing_client()
    return _list_one(card_id, db, client, settings)
