"""Review queue: step through library cards that need a human look.

GET /api/review/next walks needs_review cards in id order and explains each:
why it is in review, and for every identity field the value, its confidence
and which side of the card it was read from (front, back, both, the verifier,
or the user's own edit). POST /api/cards/{id}/confirm accepts the identity as
it stands.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.db import get_db
from app.models import (
    STATUS_DELETED,
    STATUS_LIST_FAILED,
    STATUS_LISTED,
    STATUS_NEEDS_REVIEW,
    STATUS_PREVIEW,
    Card,
    IdentificationCorrection,
)
from app.schemas import CardDetailOut
from app.services import pairing
from app.services.ebay import orders
from app.services.pricing import finalize_card, price_card

router = APIRouter(prefix="/api", tags=["review"])

REVIEW_FIELDS = (
    "player", "year", "set_brand", "card_number", "parallel", "subset", "team",
    "rookie", "serial_number", "sport",
)
# Review reasons a confirmed identity resolves. Price, grading and source
# reasons stay.
_IDENTITY_REASONS = ("low identification confidence", "incomplete identification")


def _queue(after_id: int | None = None):
    stmt = select(Card).where(Card.status == STATUS_NEEDS_REVIEW, Card.side == "front")
    if after_id is not None:
        stmt = stmt.where(Card.id > after_id)
    return stmt.options(selectinload(Card.listings), selectinload(Card.comps)).order_by(Card.id)


def _remaining(db: Session) -> int:
    return db.scalar(
        select(func.count()).select_from(Card).where(
            Card.status == STATUS_NEEDS_REVIEW, Card.side == "front"
        )
    ) or 0


def next_card(db: Session, after_id: int | None, exclude: int | None = None) -> Card | None:
    """The next card in id order after `after_id`, wrapping to the start."""
    for stmt in (_queue(after_id), _queue()):
        for card in db.scalars(stmt.limit(2)):
            if card.id != exclude:
                return card
    return None


def _audit(raw: str | None) -> dict:
    try:
        out = json.loads(raw or "{}")
    except Exception:  # noqa: BLE001
        return {}
    return out if isinstance(out, dict) else {}


def _norm(field: str, value) -> str:
    if field == "rookie":
        return "true" if str(value).strip().lower() in ("true", "1", "yes", "rc", "rookie") else "false"
    return pairing._norm_field(field, value)


def _read(reads: dict, field: str) -> dict | None:
    r = reads.get(field)
    if not isinstance(r, dict) or r.get("value") in (None, ""):
        return None
    conf = r.get("confidence")
    return {"value": r.get("value"), "confidence": conf if isinstance(conf, (int, float)) else None}


def field_provenance(card: Card, db: Session) -> list[dict]:
    """Per identity field: value, confidence and source side.

    side: "user" (the user typed this value), "both" (front and back read it),
    "front", "back", "verifier" (a verification correction set it), or None
    (no read explains it, or the field is empty)."""
    front = _audit(card.identification_json)
    back = _audit(card.back_identification_json)
    front_reads = front.get("field_reads") if isinstance(front.get("field_reads"), dict) else {}
    back_reads = back.get("field_reads") if isinstance(back.get("field_reads"), dict) else {}
    verification = front.get("verification") if isinstance(front.get("verification"), dict) else {}
    applied = verification.get("applied") if isinstance(verification.get("applied"), dict) else {}
    corrections = verification.get("corrections") if isinstance(verification.get("corrections"), dict) else {}
    typed = {
        c.field: c.final_value
        for c in db.scalars(
            select(IdentificationCorrection)
            .where(IdentificationCorrection.card_id == card.id)
            .order_by(IdentificationCorrection.id)
        )
    }
    out = []
    for field in REVIEW_FIELDS:
        value = getattr(card, field, None)
        fr, br = _read(front_reads, field), _read(back_reads, field)
        empty = value in (None, "") or (field == "rookie" and not value)
        current = None if empty else _norm(field, value)
        on_front = bool(fr and current and _norm(field, fr["value"]) == current)
        on_back = bool(br and current and _norm(field, br["value"]) == current)
        side, conf = None, None
        if field in typed and current and _norm(field, typed[field]) == current:
            side, conf = "user", 1.0
        elif on_front and on_back:
            side = "both"
            conf = max(c for c in (fr["confidence"], br["confidence"], 0.0) if c is not None)
        elif on_front:
            side, conf = "front", fr["confidence"]
        elif on_back:
            side, conf = "back", br["confidence"]
        elif field in applied and current:
            side = "verifier"
            corr = corrections.get(field) if isinstance(corrections.get(field), dict) else {}
            conf = corr.get("confidence")
        out.append({
            "field": field,
            "value": value,
            "confidence": conf,
            "side": side,
            "front_read": fr,
            "back_read": br,
        })
    return out


def review_payload(card: Card | None, db: Session) -> dict:
    remaining = _remaining(db)
    if card is None:
        return {
            "card": None, "remaining": remaining, "review_reason": None, "fields": [],
            "front_crop_url": None, "back_crop_url": None, "user_confirmed": False,
            "next_id": None,
        }
    peek = next_card(db, card.id, exclude=card.id)
    return {
        "card": CardDetailOut.model_validate(card).model_dump(mode="json"),
        "remaining": remaining,
        "review_reason": card.review_reason,
        "fields": field_provenance(card, db),
        "front_crop_url": f"/api/cards/{card.id}/crop" if card.crop_path else None,
        "back_crop_url": f"/api/cards/{card.id}/back-crop" if card.back_crop_path else None,
        "user_confirmed": "user_confirmed" in _audit(card.identification_json),
        "next_id": peek.id if peek is not None else None,
    }


@router.get("/review/next")
def review_next(
    after_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict:
    """The next library card in review (status needs_review, ordered by id,
    after `after_id`, wrapping to the start). `card` is null when the queue is
    empty. `remaining` counts the whole queue, this card included."""
    return review_payload(next_card(db, after_id), db)


def _strip_identity_reasons(reason: str | None) -> str | None:
    parts = [p.strip() for p in (reason or "").split(";") if p.strip()]
    kept = [p for p in parts if not p.startswith(_IDENTITY_REASONS)]
    return "; ".join(kept) or None


@router.post("/cards/{card_id}/confirm")
def confirm_identity(card_id: int, db: Session = Depends(get_db)) -> dict:
    """Accept the card's identity as it stands: confidence 1.0, recorded in the
    identification audit as user-confirmed (no correction row, since no field
    changed). A library card is then re-routed: priced / below_threshold, or
    needs_review only for a reason confirming cannot settle (no price yet
    found, PSA 10 candidate, anomaly, missing identity, a price source down).
    A card never priced because its confidence was too low is priced now.

    Returns {"card", "next_id", "remaining"}."""
    card = db.get(Card, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    if card.status == STATUS_DELETED:
        raise HTTPException(status_code=409, detail="This card is deleted; restore it first")
    audit = _audit(card.identification_json)
    audit["user_confirmed"] = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "previous_confidence": card.confidence,
    }
    card.identification_json = json.dumps(audit)
    card.confidence = 1.0
    card.review_reason = _strip_identity_reasons(card.review_reason)
    on_ebay = card.status in (STATUS_LISTED, STATUS_LIST_FAILED) or orders.listing_state(card) in (
        orders.STATE_LIVE, orders.STATE_SOLD
    )
    if card.status != STATUS_PREVIEW and not on_ebay:
        if card.estimated_price is None and card.player and (card.year or card.set_brand):
            db.commit()  # save the confirmation before the comp fetch
            price_card(card, db, commit_after_fetch=True)
        else:
            finalize_card(card, get_settings())
    db.commit()
    nxt = next_card(db, card.id, exclude=card.id)
    return {
        "card": CardDetailOut.model_validate(card).model_dump(mode="json"),
        "next_id": nxt.id if nxt is not None else None,
        "remaining": _remaining(db),
    }
