"""After a card is listed: its listing state, ending it, and learning it sold.

- listing_state(card)          -> "none" | "live" | "ended" | "sold"
- end_listing_for_card(db, c)  -> withdraws the card's live eBay offer and marks
                                  its Listing rows "ended" (every card of a lot)
- sync_sold(db)                -> reads eBay orders (Fulfillment API getOrders),
                                  matches line items to listings by SKU (or the
                                  eBay item id), marks those rows "sold" with the
                                  sale date, price and order id

The sold sync needs the sell.fulfillment scope. A refresh token minted before
that scope was added must be re-authorized once at /ebay/oauth/start.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    LISTING_ENDED,
    LISTING_PUBLISHED,
    LISTING_SOLD,
    Listing,
)
from app.services.ebay.oauth import FULFILLMENT_SCOPES, get_user_access_token
from app.services.ebay.sandbox import SandboxEbayClient, _raise_ebay

logger = logging.getLogger("ebay.orders")

LIVE_API = "https://api.ebay.com"
SANDBOX_API = "https://api.sandbox.ebay.com"
_PAGE = 200  # getOrders maximum
_MAX_LOOKBACK = timedelta(days=720)  # getOrders returns orders up to 2 years old
_REAL_MODES = ("live", "sandbox")

STATE_NONE = "none"
STATE_LIVE = "live"
STATE_ENDED = "ended"
STATE_SOLD = "sold"


def listing_state(card) -> str:
    """Where a card stands on eBay: sold beats live beats ended beats none.
    Preview and failed rows never went live, so they count as none."""
    statuses = {getattr(row, "status", None) for row in (getattr(card, "listings", None) or [])}
    if LISTING_SOLD in statuses:
        return STATE_SOLD
    if LISTING_PUBLISHED in statuses:
        return STATE_LIVE
    if LISTING_ENDED in statuses:
        return STATE_ENDED
    return STATE_NONE


def live_listing(card) -> Listing | None:
    """The card's newest published Listing row, if it is live."""
    rows = [r for r in (card.listings or []) if r.status == LISTING_PUBLISHED]
    return max(rows, key=lambda r: (r.created_at or datetime.min, r.id or 0)) if rows else None


def _rows_for_offer(db: Session, row: Listing) -> list[Listing]:
    """Every published row sharing this listing (one per card of a lot)."""
    if not row.offer_id:
        return [row]
    return list(db.scalars(
        select(Listing).where(
            Listing.offer_id == row.offer_id,
            Listing.ebay_mode == row.ebay_mode,
            Listing.status == LISTING_PUBLISHED,
        )
    ))


def _client_for(mode: str):
    return SandboxEbayClient(live=mode == "live")


def end_listing_for_card(db: Session, card) -> dict:
    """End the card's live eBay listing, if it has one.

    Call this before deleting a listed card so a deleted card cannot still sell
    on eBay. Returns {"ended": bool, "card_ids": [...], "message": str}. Raises
    if eBay refuses (the caller decides whether to block the delete). Ending a
    lot ends it for every card in the lot.
    """
    row = live_listing(card)
    if row is None:
        return {"ended": False, "card_ids": [], "message": "card has no live eBay listing"}
    if row.ebay_mode in _REAL_MODES and row.offer_id:
        _client_for(row.ebay_mode).end_offer(row.offer_id)
    now = datetime.now(timezone.utc)
    rows = _rows_for_offer(db, row)
    for r in rows:
        r.status = LISTING_ENDED
        r.ended_at = now
    db.commit()
    ids = sorted({r.card_id for r in rows})
    msg = "Ended the eBay listing." if len(ids) == 1 else f"Ended the eBay lot listing ({len(ids)} cards)."
    return {"ended": True, "card_ids": ids, "message": msg}


# --- sold sync -------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _parse_time(raw) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_orders(live: bool, since: datetime) -> list[dict]:
    """All orders modified since `since` (paged through getOrders)."""
    token = get_user_access_token(live=live, scope=FULFILLMENT_SCOPES)
    headers = {"Authorization": f"Bearer {token}"}
    orders: list[dict] = []
    offset = 0
    with httpx.Client(base_url=LIVE_API if live else SANDBOX_API, timeout=60) as client:
        while True:
            resp = client.get(
                "/sell/fulfillment/v1/order",
                headers=headers,
                params={
                    "filter": f"lastmodifieddate:[{_iso(since)}..]",
                    "limit": _PAGE,
                    "offset": offset,
                },
            )
            _raise_ebay(resp, "read orders")
            body = resp.json()
            page = body.get("orders") or []
            orders += page
            offset += len(page)
            if not page or offset >= int(body.get("total") or 0):
                break
    return orders


def _money(obj) -> float | None:
    try:
        return float((obj or {}).get("value"))
    except (TypeError, ValueError):
        return None


def sync_sold(db: Session, since: datetime | None = None) -> dict:
    """Mark listings sold from eBay orders. Returns
    {"orders_checked": int, "sold": [{"card_id", "order_id", "sold_price", "sold_at"}],
     "since": iso, "errors": [str]}."""
    live_rows = list(db.scalars(
        select(Listing).where(
            Listing.status == LISTING_PUBLISHED, Listing.ebay_mode.in_(_REAL_MODES)
        )
    ))
    result = {"orders_checked": 0, "sold": [], "since": None, "errors": []}
    if not live_rows:
        return result

    now = datetime.now(timezone.utc)
    if since is None:
        oldest = min((r.created_at for r in live_rows if r.created_at), default=now)
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        since = oldest - timedelta(days=1)
    since = max(since, now - _MAX_LOOKBACK)
    result["since"] = _iso(since)

    for mode in sorted({r.ebay_mode for r in live_rows}):
        rows = [r for r in live_rows if r.ebay_mode == mode]
        by_sku: dict[str, list[Listing]] = {}
        by_item: dict[str, list[Listing]] = {}
        for r in rows:
            if r.sku:
                by_sku.setdefault(r.sku, []).append(r)
            if r.listing_id:
                by_item.setdefault(str(r.listing_id), []).append(r)
        try:
            orders = fetch_orders(mode == "live", since)
        except Exception as exc:  # noqa: BLE001
            logger.exception("sold sync failed for %s", mode)
            result["errors"].append(f"{mode}: {exc}")
            continue
        result["orders_checked"] += len(orders)
        for order in orders:
            cancel = ((order.get("cancelStatus") or {}).get("cancelState") or "").upper()
            if cancel == "CANCELED":
                continue
            sold_at = _parse_time(order.get("creationDate")) or now
            for item in order.get("lineItems") or []:
                matched = by_sku.get(item.get("sku") or "") or by_item.get(
                    str(item.get("legacyItemId") or "")
                ) or []
                price = _money(item.get("total")) or _money(item.get("lineItemCost"))
                for r in matched:
                    if r.status != LISTING_PUBLISHED:
                        continue
                    r.status = LISTING_SOLD
                    r.sold_at = sold_at
                    r.sold_price = price
                    r.order_id = order.get("orderId")
                    result["sold"].append({
                        "card_id": r.card_id, "order_id": r.order_id,
                        "sold_price": price, "sold_at": sold_at.isoformat(),
                    })
    db.commit()
    return result
