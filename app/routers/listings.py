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
    STATUS_PREVIEW,
    Card,
    Listing,
)
from app.schemas import (
    SellRequest,
    SellResponse,
    SellResult,
    SetSellResult,
)
from app.services.ebay.factory import get_listing_client

logger = logging.getLogger("listings")
router = APIRouter(tags=["listings"])

# Listing is only ever triggered by an EXPLICIT user request (a list of card_ids
# the user picked) — never automatically. So the only hard requirements are that
# the card is in the library (not an un-added preview) and has a real price. The
# user may deliberately list a below-threshold or needs-review card they picked.
NOT_LISTABLE = {STATUS_PREVIEW}


def _list_one(card_id: int, db: Session, client, settings) -> SellResult:
    card = db.get(Card, card_id)
    if card is None:
        return SellResult(card_id=card_id, status="skipped", message="not found")
    if card.status in NOT_LISTABLE:
        return SellResult(
            card_id=card_id, status="skipped",
            message=f"not in library (status={card.status})",
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
    # "Listed on eBay" is tracked separately (via the published Listing row /
    # card.is_listed) so it coexists with the card's price status instead of
    # overwriting it — a listed card is still 'priced' or 'below_threshold'.
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


def _collect_sellable(card_ids: list[int], db: Session) -> tuple[list[Card], list[str]]:
    """Split requested cards into sellable ones and human-readable skip reasons."""
    cards: list[Card] = []
    skipped: list[str] = []
    for cid in card_ids:
        card = db.get(Card, cid)
        if card is None:
            skipped.append(f"card {cid}: not found")
        elif card.status in NOT_LISTABLE:
            skipped.append(f"card {cid}: not in library (status={card.status})")
        elif not card.estimated_price:
            skipped.append(f"card {cid}: no estimated price")
        else:
            cards.append(card)
    return cards, skipped


@router.post("/api/listings/sell-set", response_model=SetSellResult)
def sell_set(req: SellRequest, db: Session = Depends(get_db)) -> SetSellResult:
    """List the selected cards as ONE combined lot listing (all cards + photos).

    Price = the summed per-card list prices. On success, every included card gets
    a Listing row sharing the lot's sku/offer/listing id, so each shows as listed.
    """
    settings = get_settings()
    cards, skipped = _collect_sellable(req.card_ids, db)
    if not cards:
        return SetSellResult(
            status="skipped", skipped=skipped,
            message="No sellable cards in the selection.",
        )

    set_price = round(
        sum(c.estimated_price * settings.price_markup for c in cards), 2
    )
    client = get_listing_client()
    card_ids = [c.id for c in cards]
    try:
        result = client.create_set_listing(cards, set_price)
    except Exception as exc:  # noqa: BLE001
        logger.exception("set listing failed for cards %s", card_ids)
        return SetSellResult(
            status="failed", list_price=set_price, card_ids=card_ids,
            skipped=skipped, message=str(exc),
        )

    # One Listing row per card, all sharing the lot's identifiers, so each card's
    # is_listed flips and the repository marks the whole lot as listed.
    for card in cards:
        db.add(Listing(
            card_id=card.id, ebay_mode=settings.ebay_mode, sku=result.sku,
            offer_id=result.offer_id, listing_id=result.listing_id,
            list_price=result.list_price, status=result.status,
            response_json=json.dumps(result.response),
        ))
    db.commit()
    return SetSellResult(
        status=result.status, listing_id=result.listing_id, sku=result.sku,
        list_price=result.list_price, card_ids=card_ids, skipped=skipped,
        message=result.message,
    )
