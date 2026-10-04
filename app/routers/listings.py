"""Listing endpoints: create, revise, end and track eBay Buy-It-Now listings.

- POST /api/listings/sell             batch (list of card_ids), per-card results
- POST /api/cards/{id}/list           single card ("List on eBay" button)
- POST /api/listings/sell-set         several cards as ONE lot listing
- GET  /api/listings/{id}             listing state + suggested price for the UI
- POST /api/listings/{id}/price       change a live listing's price
- POST /api/listings/{id}/end         end (withdraw) a live listing
- POST /api/listings/sync-sold        mark listings sold from eBay orders

A listing is only attempted for cards in a sellable status that are not already
live or sold on eBay (a live card is revised through /price instead, so a card
can never be listed twice). With EBAY_MODE in its default `preview`, nothing is
published: the result has status="preview". Only a real `published` result
makes a card live.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import (
    LISTING_FAILED,
    LISTING_PUBLISHED,
    LISTING_SOLD,
    STATUS_PREVIEW,
    Card,
    Listing,
)
from app.schemas import (
    SellRequest,
    SellResult,
    SetSellResult,
)
from app.services.ebay.factory import get_listing_client
from app.services.ebay.listing_common import (
    listing_price_floor,
    suggested_list_price,
    suggested_lot_price,
)
from app.services.ebay.orders import (
    STATE_LIVE,
    STATE_SOLD,
    _client_for,
    end_listing_for_card,
    listing_state,
    live_listing,
    sync_sold,
)

logger = logging.getLogger("listings")
router = APIRouter(tags=["listings"])

# Listing is only ever triggered by an EXPLICIT user request (a list of card_ids
# the user picked), never automatically. So the only hard requirements are that
# the card is in the library (not an un-added preview) and has a real price. The
# user may deliberately list a below-threshold or needs-review card they picked.
NOT_LISTABLE = {STATUS_PREVIEW}


class BulkSellResult(SellResult):
    """One card's outcome in a batch, with enough for the UI to show why."""

    ok: bool = False
    error: str | None = None
    listing_url: str | None = None


class BulkSellResponse(BaseModel):
    results: list[BulkSellResult]


class PriceUpdateRequest(BaseModel):
    # Blank = use the suggested list price.
    price: float | None = Field(default=None, gt=0)


def _listing_url(mode: str, listing_id: str | None) -> str | None:
    if not listing_id:
        return None
    host = "www.ebay.com" if mode == "live" else "sandbox.ebay.com"
    return f"https://{host}/itm/{listing_id}"


def _bulk(result: SellResult, mode: str) -> BulkSellResult:
    ok = result.status in (LISTING_PUBLISHED, "preview")
    return BulkSellResult(
        **result.model_dump(),
        ok=ok,
        error=None if ok else result.message,
        listing_url=_listing_url(mode, result.listing_id) if result.status == LISTING_PUBLISHED else None,
    )


def _why_not_listable(card: Card | None, card_id: int) -> tuple[str, str] | None:
    """(kind, message) when this card cannot be listed now, else None.
    kind is "skipped" or "conflict" (already on eBay)."""
    if card is None:
        return "skipped", "not found"
    if card.status in NOT_LISTABLE:
        return "skipped", f"not in library (status={card.status})"
    state = listing_state(card)
    if state == STATE_LIVE:
        return "conflict", (
            "already listed on eBay; change its price with "
            f"POST /api/listings/{card_id}/price or end it first"
        )
    if state == STATE_SOLD:
        return "conflict", "already sold on eBay"
    if not card.estimated_price:
        return "skipped", "no estimated price"
    return None


def _check_price(price: float, settings) -> str | None:
    floor = listing_price_floor(settings)
    if price < floor:
        return (
            f"price ${price:.2f} is below the ${floor:.2f} floor (eBay fees + "
            "shipping supplies + minimum net)"
        )
    return None


def _list_one(
    card_id: int, db: Session, client, settings,
    override_price: float | None = None,
) -> SellResult:
    card = db.get(Card, card_id)
    blocked = _why_not_listable(card, card_id)
    if blocked:
        return SellResult(card_id=card_id, status="skipped", message=blocked[1])

    list_price = round(override_price, 2) if override_price else suggested_list_price(card, settings)
    too_low = _check_price(list_price, settings)
    if too_low:
        return SellResult(card_id=card_id, status="skipped", list_price=list_price, message=too_low)
    try:
        result = client.create_listing(card, list_price)
    except Exception as exc:  # noqa: BLE001
        logger.exception("listing failed for card %s", card_id)
        db.add(Listing(
            card_id=card_id, ebay_mode=settings.ebay_mode, list_price=list_price,
            status=LISTING_FAILED, response_json=json.dumps({"error": str(exc)}),
        ))
        # A failed publish attempt does not corrupt the card's priced state.
        db.commit()
        return SellResult(card_id=card_id, status="failed", list_price=list_price, message=str(exc))

    db.add(Listing(
        card_id=card_id, ebay_mode=settings.ebay_mode, sku=result.sku,
        offer_id=result.offer_id, listing_id=result.listing_id,
        list_price=result.list_price, status=result.status,
        response_json=json.dumps(result.response),
    ))
    # "Listed on eBay" is tracked separately (via the published Listing row /
    # card.is_listed) so it coexists with the card's price status instead of
    # overwriting it: a listed card is still 'priced' or 'below_threshold'.
    db.commit()
    return SellResult(
        card_id=card_id, status=result.status, listing_id=result.listing_id,
        list_price=result.list_price, message=result.message,
    )


@router.post("/api/listings/sell", response_model=BulkSellResponse)
def sell(req: SellRequest, db: Session = Depends(get_db)) -> BulkSellResponse:
    """List each selected card on its own. Every card gets a result with
    ok / error / listing_url so the UI can say exactly which failed and why."""
    settings = get_settings()
    client = get_listing_client()
    prices = req.prices or {}
    return BulkSellResponse(results=[
        _bulk(_list_one(cid, db, client, settings, override_price=prices.get(str(cid))),
              settings.ebay_mode)
        for cid in req.card_ids
    ])


@router.post("/api/cards/{card_id}/list", response_model=SellResult)
def list_one(card_id: int, db: Session = Depends(get_db)):
    """Create an eBay listing for a single card (the 'List on eBay' button).
    409 when the card is already live or sold on eBay."""
    blocked = _why_not_listable(db.get(Card, card_id), card_id)
    if blocked and blocked[0] == "conflict":
        return JSONResponse(
            status_code=409,
            content=SellResult(card_id=card_id, status="skipped", message=blocked[1]).model_dump(),
        )
    settings = get_settings()
    return _list_one(card_id, db, get_listing_client(), settings)


def _collect_sellable(card_ids: list[int], db: Session) -> tuple[list[Card], list[str], list[str]]:
    """Split requested cards into sellable ones, human-readable skip reasons, and
    conflicts (cards already live or sold, which block the whole lot)."""
    cards: list[Card] = []
    skipped: list[str] = []
    conflicts: list[str] = []
    for cid in card_ids:
        card = db.get(Card, cid)
        blocked = _why_not_listable(card, cid)
        if blocked is None:
            cards.append(card)
        elif blocked[0] == "conflict":
            conflicts.append(f"card {cid}: {blocked[1]}")
        else:
            skipped.append(f"card {cid}: {blocked[1]}")
    return cards, skipped, conflicts


@router.post("/api/listings/sell-set", response_model=SetSellResult)
def sell_set(req: SellRequest, db: Session = Depends(get_db)) -> SetSellResult:
    """List the selected cards as ONE combined lot listing (all cards + photos).

    Price = suggested_lot_price (the cards' base prices summed, floor applied
    once). A selection containing a card already live or sold on eBay is
    refused (status "blocked"). On success every included card gets a Listing
    row sharing the lot's sku/offer/listing id; on failure every card gets a
    "failed" row recording eBay's message.
    """
    settings = get_settings()
    cards, skipped, conflicts = _collect_sellable(req.card_ids, db)
    if conflicts:
        return SetSellResult(
            status="blocked", skipped=skipped + conflicts,
            message="Some selected cards are already on eBay: " + "; ".join(conflicts),
        )
    if not cards:
        return SetSellResult(
            status="skipped", skipped=skipped,
            message="No sellable cards in the selection.",
        )

    prices = req.prices or {}
    if prices.get("set"):
        set_price = round(float(prices["set"]), 2)
    else:
        set_price = suggested_lot_price(cards, settings)
    card_ids = [c.id for c in cards]
    too_low = _check_price(set_price, settings)
    if too_low:
        return SetSellResult(
            status="skipped", list_price=set_price, card_ids=card_ids,
            skipped=skipped, message=too_low,
        )
    client = get_listing_client()
    try:
        result = client.create_set_listing(cards, set_price)
    except Exception as exc:  # noqa: BLE001
        logger.exception("set listing failed for cards %s", card_ids)
        for card in cards:
            db.add(Listing(
                card_id=card.id, ebay_mode=settings.ebay_mode, list_price=set_price,
                status=LISTING_FAILED,
                response_json=json.dumps({"error": str(exc), "lot_card_ids": card_ids}),
            ))
        db.commit()
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


# --- after listing ------------------------------------------------------------------


def listing_info(card: Card, settings) -> dict:
    """What the UI needs about a card's eBay listing (GET /api/listings/{id})."""
    live = live_listing(card)
    sold = next((r for r in card.listings if r.status == LISTING_SOLD), None)
    current = live or sold
    return {
        "card_id": card.id,
        "listing_state": listing_state(card),
        "listing_url": _listing_url(current.ebay_mode, current.listing_id) if current else None,
        "list_price": current.list_price if current else None,
        "suggested_list_price": suggested_list_price(card, settings),
        "price_floor": listing_price_floor(settings),
        "ebay_mode": current.ebay_mode if current else None,
        "sku": current.sku if current else None,
        "offer_id": current.offer_id if current else None,
        "listing_id": current.listing_id if current else None,
        "sold_at": sold.sold_at.isoformat() if sold and sold.sold_at else None,
        "sold_price": sold.sold_price if sold else None,
    }


def _card_or_404(db: Session, card_id: int) -> Card:
    card = db.get(Card, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="card not found")
    return card


@router.post("/api/listings/sync-sold")
def sync_sold_endpoint(db: Session = Depends(get_db)) -> dict:
    """Ask eBay which listed cards sold and mark them sold. Response:
    {"orders_checked": int, "sold": [{card_id, order_id, sold_price, sold_at}],
     "since": iso, "errors": [str]}"""
    return sync_sold(db)


@router.get("/api/listings/{card_id}")
def get_listing(card_id: int, db: Session = Depends(get_db)) -> dict:
    return listing_info(_card_or_404(db, card_id), get_settings())


@router.post("/api/listings/{card_id}/end")
def end_listing(card_id: int, db: Session = Depends(get_db)) -> dict:
    """End (withdraw) the card's live eBay listing. 409 when it is not live;
    502 with eBay's message when eBay refuses."""
    card = _card_or_404(db, card_id)
    if listing_state(card) != STATE_LIVE:
        raise HTTPException(status_code=409, detail="card has no live eBay listing")
    try:
        out = end_listing_for_card(db, card)
    except Exception as exc:  # noqa: BLE001
        logger.exception("ending listing failed for card %s", card_id)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {**out, **listing_info(card, get_settings())}


@router.post("/api/listings/{card_id}/price")
def update_listing_price(
    card_id: int, req: PriceUpdateRequest | None = None, db: Session = Depends(get_db),
) -> dict:
    """Change a live listing's price (blank = the suggested list price). Best
    Offer terms follow the new price. 400 below the floor, 409 when not live."""
    settings = get_settings()
    card = _card_or_404(db, card_id)
    row = live_listing(card)
    if row is None:
        raise HTTPException(status_code=409, detail="card has no live eBay listing")
    price = round(req.price, 2) if req and req.price else suggested_list_price(card, settings)
    if price is None:
        raise HTTPException(status_code=400, detail="no price given and no estimate to suggest one")
    too_low = _check_price(price, settings)
    if too_low:
        raise HTTPException(status_code=400, detail=too_low)
    if row.ebay_mode in ("live", "sandbox") and row.offer_id:
        try:
            _client_for(row.ebay_mode).update_price(row.offer_id, price)
        except Exception as exc:  # noqa: BLE001
            logger.exception("price update failed for card %s", card_id)
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    row.list_price = price
    if row.offer_id:
        # A lot's other cards share the offer; keep their rows in step.
        for other in db.query(Listing).filter(
            Listing.offer_id == row.offer_id, Listing.status == LISTING_PUBLISHED
        ):
            other.list_price = price
    db.commit()
    return {"ok": True, "message": f"Price changed to ${price:.2f}.", **listing_info(card, settings)}
