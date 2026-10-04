"""Sold sales recorded by hand for one card (POST /api/cards/{id}/sold-sales).

The automatic sold sources can be down (an expired token, a site blocking
scripts). A person, or Claude driving a logged-in browser, can still read eBay's
sold listings; those sales are stored here and added to every price run for the
card as ordinary sold comps, so matching, recency and outlier trimming all apply.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import RecordedSale
from app.services.ebay.base import SoldComp

DEFAULT_SOURCE = "ebay sold (looked up)"


def comps_for(db: Session, card) -> list[SoldComp]:
    if getattr(card, "id", None) is None:
        return []
    rows = db.scalars(select(RecordedSale).where(RecordedSale.card_id == card.id)).all()
    return [
        SoldComp(
            title=r.title, sold_price=r.sold_price, sold_date=r.sold_date,
            listing_url=r.listing_url, source=r.source or DEFAULT_SOURCE,
            marketplace="eBay", kind="sold",
        )
        for r in rows
    ]


def record(db: Session, card, sales: list[dict], *, replace: bool = False) -> int:
    """Store sales for a card; returns how many were added. A sale already
    stored (same URL, or same title, price and date) is not added twice.
    `replace` drops the card's earlier recorded sales first."""
    existing = list(db.scalars(select(RecordedSale).where(RecordedSale.card_id == card.id)).all())
    if replace:
        for r in existing:
            db.delete(r)
        existing = []
    seen = {(r.listing_url or "", r.title, round(r.sold_price, 2), r.sold_date) for r in existing}
    seen_urls = {r.listing_url for r in existing if r.listing_url}
    added = 0
    for s in sales:
        title = (s.get("title") or "").strip()
        price = s.get("price")
        if not title or price is None or float(price) <= 0:
            continue
        url = (s.get("url") or "").strip() or None
        key = (url or "", title, round(float(price), 2), s.get("date"))
        if key in seen or (url and url in seen_urls):
            continue
        seen.add(key)
        if url:
            seen_urls.add(url)
        db.add(RecordedSale(
            card_id=card.id, title=title, sold_price=round(float(price), 2),
            sold_date=s.get("date"), listing_url=url,
            source=(s.get("source") or DEFAULT_SOURCE)[:48],
        ))
        added += 1
    db.flush()
    return added
