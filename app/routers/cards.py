"""Read endpoints for the card repository + per-card transparency detail."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import CROPS_DIR, get_settings
from app.db import get_db
from app.models import STATUS_PREVIEW, Card, ImageUpload
from app.routers.upload import _apply_detection
from app.schemas import (
    CardDetailOut,
    CardOut,
    CardUpdateRequest,
    ManualCardRequest,
    PriceFromUrlRequest,
    PromoteRequest,
)
from app.services import cropping, pairing, photo_archive, pricecharting, vision
from app.services.pricing import finalize_card, price_card, price_from_url, preview_card

logger = logging.getLogger("cards")
router = APIRouter(prefix="/api/cards", tags=["cards"])


def _card_description(card: Card) -> str:
    """Build a human-readable description from card metadata for archive filenames."""
    parts: list[str] = []
    if card.player:
        parts.append(card.player)
    if card.set_brand:
        parts.append(card.set_brand)
    if card.year:
        parts.append(card.year)
    if card.parallel:
        parts.append(card.parallel)
    return ", ".join(parts) or "card"


@router.post("/manual", response_model=CardDetailOut)
def add_manual(req: ManualCardRequest, db: Session = Depends(get_db)) -> Card:
    """Add a card by typing its identity (no photo / no Anthropic key needed),
    then price it from real comps just like an uploaded card."""
    if not req.player or not req.player.strip():
        raise HTTPException(status_code=422, detail="Player is required.")
    if not ((req.year and req.year.strip()) or (req.set_brand and req.set_brand.strip())):
        raise HTTPException(status_code=422, detail="Provide at least a year or a set.")

    upload = ImageUpload(filename="manual entry", card_count=1)
    db.add(upload)
    db.flush()
    card = Card(
        upload_id=upload.id,
        player=req.player.strip(),
        year=req.year,
        sport=(req.sport or "").strip().lower() or None,
        set_brand=req.set_brand,
        card_number=req.card_number,
        parallel=req.parallel,
        serial_number=req.serial_number,
        condition=req.condition,
        confidence=1.0,  # user-entered identity is taken as certain
        psa10_candidate=req.psa10_candidate,
        anomaly_flag=req.anomaly_flag,
    )
    db.add(card)
    db.flush()
    price_card(card, db)
    db.commit()
    return card


@router.post("/promote", response_model=list[CardOut])
def promote_cards(req: PromoteRequest, db: Session = Depends(get_db)) -> list[Card]:
    """Add previewed cards to the repository. Each card runs the normal safeguard
    gating + status routing (priced / needs_review / below_threshold), reusing the
    estimate already computed at preview time."""
    settings = get_settings()
    promoted: list[Card] = []
    to_archive: list[tuple[str | None, str, str]] = []
    crops_to_archive: list[tuple[str | None, str, str]] = []
    for card_id in req.card_ids:
        card = db.get(Card, card_id)
        if card is None:
            continue
        if card.status == STATUS_PREVIEW:
            finalize_card(card, settings)
            desc = _card_description(card)
            # Archive original source photo(s) (moved out of inbox).
            up = db.get(ImageUpload, card.upload_id) if card.upload_id else None
            if up:
                to_archive.append((up.filename, desc, "front"))
            try:
                audit = json.loads(card.back_identification_json or "{}")
                if isinstance(audit, dict) and audit.get("_source_filename"):
                    to_archive.append((audit["_source_filename"], desc, "back"))
            except Exception:  # noqa: BLE001
                pass
            # Copy crop images (front + back) into the collection folder.
            if card.crop_path:
                crops_to_archive.append((card.crop_path, desc, "front"))
            if card.back_crop_path:
                crops_to_archive.append((card.back_crop_path, desc, "back"))
        promoted.append(card)
    db.commit()
    if to_archive:
        try:
            photo_archive.archive_source_files(to_archive)
        except Exception:  # noqa: BLE001
            logger.exception("source-photo archive step failed")
    if crops_to_archive:
        try:
            photo_archive.archive_crop_files(crops_to_archive)
        except Exception:  # noqa: BLE001
            logger.exception("crop-photo archive step failed")
    try:
        photo_archive.backup_database()
    except Exception:  # noqa: BLE001
        logger.exception("database backup step failed")
    return promoted


@router.post("/{card_id}/reanalyze", response_model=CardDetailOut)
def reanalyze_card(card_id: int, db: Session = Depends(get_db)) -> Card:
    """Re-run identification on a previewed card's crop using the strongest
    available model (Claude when an Anthropic key is set, else high-quality
    Gemini), then re-price it. The card stays in preview for the user to review."""
    card = db.get(Card, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    if card.status != STATUS_PREVIEW:
        raise HTTPException(
            status_code=409, detail="Only a preview card can be re-analyzed."
        )
    if not card.crop_path or not Path(card.crop_path).exists():
        raise HTTPException(
            status_code=422,
            detail="No crop available to re-analyze. Add the card manually instead.",
        )

    try:
        provider, model, _label = vision.strong_backend()
        crop_bytes = cropping.read_crop_bytes(card.crop_path)
        det = vision.reidentify(crop_bytes, provider=provider, model=model)
    except vision.MissingVisionKeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("re-analysis failed for card %s", card.id)
        raise HTTPException(status_code=502, detail=f"Re-analysis failed: {exc}")
    if det is None:
        raise HTTPException(
            status_code=422, detail="Re-analysis could not read a card in the crop."
        )

    # Apply the fresh identity (keep the existing crop/bbox).
    bbox = card.bbox_json
    _apply_detection(card, det)
    card.bbox_json = bbox

    # Drop the previous comps + pricing and re-price from the new identity.
    for comp in list(card.comps):
        db.delete(comp)
    for field in (
        "estimated_price", "raw_value_estimate", "graded_value_estimate",
        "sold_estimate", "active_estimate", "price_basis", "price_source",
        "derivation", "price_sources", "reference_image_url", "review_reason",
    ):
        setattr(card, field, None)
    card.excluded_count = 0
    preview_card(card, db)
    db.commit()
    return card


@router.delete("/{card_id}", status_code=204)
def discard_card(card_id: int, db: Session = Depends(get_db)) -> None:
    """Delete a card. Works for both un-added previews (the upload "Discard"
    action) and cards already in the repository (the library "Delete" action).
    Its comps and listing records are removed too (cascade) and its crop file is
    cleaned up off disk. Note: a card already published to eBay is removed from
    this app only — it is NOT de-listed on eBay."""
    card = db.get(Card, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    crop_path = card.crop_path
    back_crop_path = card.back_crop_path
    db.delete(card)
    db.commit()
    cropping.delete_crop(crop_path)
    if back_crop_path:
        cropping.delete_crop(back_crop_path)


@router.get("/stats")
def collection_stats(db: Session = Depends(get_db)) -> dict:
    """Collection-wide KPIs for the top of the My Collection page."""
    s = get_settings()
    cards = list(
        db.scalars(
            select(Card)
            .options(selectinload(Card.listings))
            .where(Card.side == "front", Card.status != STATUS_PREVIEW)
        ).all()
    )
    priced = [c for c in cards if c.estimated_price is not None]

    def list_price(c: Card) -> float:
        return round((c.estimated_price or 0) * s.price_markup, 2)

    def max_value(c: Card) -> float:
        return c.sold_max_estimate or c.estimated_price or 0

    def sell_expense(price: float) -> float:
        # Fee on the sale price + per-order fee + shipping supplies.
        return price * s.ebay_fee_pct + s.ebay_per_order_fee + s.supplies_cost_per_card

    total_value = sum(c.estimated_price for c in priced)
    total_max_value = sum(max_value(c) for c in priced)
    listed = [c for c in cards if c.is_listed]
    active_listings_value = sum(list_price(c) for c in listed)
    # Projected selling costs if every priced card sold at its MAX value.
    selling_expenses = sum(sell_expense(max_value(c)) for c in priced)

    return {
        "card_count": len(cards),
        "priced_count": len(priced),
        "listed_count": len(listed),
        "total_value": round(total_value, 2),
        "total_max_value": round(total_max_value, 2),
        "selling_expenses": round(selling_expenses, 2),
        "active_listings_value": round(active_listings_value, 2),
        "psa10_count": sum(1 for c in cards if c.psa10_candidate),
    }


@router.get("", response_model=list[CardOut])
def list_cards(
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[Card]:
    # Eager-load listings so card.is_listed doesn't trigger a query per card.
    stmt = select(Card).options(selectinload(Card.listings)).order_by(Card.created_at.desc())
    if status == "unmatched_backs":
        # The dedicated view for back scans that didn't pair to a front.
        return list(db.scalars(stmt.where(Card.side == "back")).all())
    # The collection shows fronts only; un-matched "back" rows stay hidden until
    # a matching front absorbs them (or are managed in the unmatched-backs view).
    stmt = stmt.where(Card.side == "front")
    if status:
        stmt = stmt.where(Card.status == status)
    else:
        # Default views never include un-added previews.
        stmt = stmt.where(Card.status != STATUS_PREVIEW)
    return list(db.scalars(stmt).all())


@router.get("/{card_id}", response_model=CardDetailOut)
def get_card(card_id: int, db: Session = Depends(get_db)) -> Card:
    card = db.get(Card, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    return card


@router.patch("/{card_id}", response_model=CardDetailOut)
def update_card(
    card_id: int, req: CardUpdateRequest, db: Session = Depends(get_db)
) -> Card:
    """Update editable metadata fields on a card. Only fields explicitly sent
    (non-None) are applied. After saving, re-prices the card from the new identity."""
    card = db.get(Card, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    changed = False
    for field in (
        "player", "year", "sport", "set_brand", "card_number",
        "parallel", "serial_number", "condition",
    ):
        val = getattr(req, field)
        if val is not None:
            cleaned = val.strip() if isinstance(val, str) else val
            if field == "sport":
                cleaned = cleaned.lower() if cleaned else None
            setattr(card, field, cleaned or None)
            changed = True
    for field in ("psa10_candidate", "anomaly_flag"):
        val = getattr(req, field)
        if val is not None:
            setattr(card, field, val)
            changed = True
    if changed:
        for comp in list(card.comps):
            db.delete(comp)
        db.flush()
        price_card(card, db, refresh=True)
    db.commit()
    return card


@router.post("/{card_id}/replace-photo", response_model=CardDetailOut)
def replace_photo(
    card_id: int,
    side: str = Query(default="front"),
    db: Session = Depends(get_db),
    file: UploadFile = File(...),
) -> Card:
    """Replace the front or back photo for a card."""
    card = db.get(Card, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    if side not in ("front", "back"):
        raise HTTPException(status_code=422, detail="side must be 'front' or 'back'")
    content = file.file.read()
    if not content:
        raise HTTPException(status_code=422, detail="Empty file")
    import uuid
    ext = Path(file.filename or "photo.jpg").suffix or ".jpg"
    new_path = str(CROPS_DIR / f"{card_id}-{uuid.uuid4().hex[:8]}{ext}")
    Path(new_path).write_bytes(content)
    if side == "front":
        cropping.delete_crop(card.crop_path)
        card.crop_path = new_path
    else:
        cropping.delete_crop(card.back_crop_path)
        card.back_crop_path = new_path
    db.commit()
    return card


@router.get("/{card_id}/crop")
def get_card_crop(card_id: int, db: Session = Depends(get_db)) -> FileResponse:
    card = db.get(Card, card_id)
    if card is None or not card.crop_path:
        raise HTTPException(status_code=404, detail="Crop not found")
    path = Path(card.crop_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Crop file missing")
    stat = path.stat()
    etag = f'"{card_id}-{int(stat.st_mtime)}"'
    return FileResponse(
        path,
        headers={
            "Cache-Control": "no-cache",
            "ETag": etag,
        },
    )


@router.post("/{card_id}/mark-back")
def mark_as_back(card_id: int, db: Session = Depends(get_db)) -> dict:
    """Reclassify a card the AI mislabeled as a front into a BACK. It leaves the
    collection (which shows fronts only) and tries to pair to its matching front;
    if none is found it becomes an un-matched back to attach manually."""
    card = db.get(Card, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    card.side = "back"
    card.status = STATUS_PREVIEW  # backs aren't part of the priced collection
    front = pairing.try_pair(card, db)  # as a back, attach to a matching front
    merged_into = front.id if front is not None else None
    db.commit()
    return {"merged_into": merged_into}


def _attach_back(front: Card, back: Card, db: Session) -> Card:
    """Core attach: move `back`'s image onto `front`, enrich + re-price, delete
    the back row. Coerces sides so it works even when the AI mislabeled them."""
    front.side = "front"
    back.side = "back"
    # If the front already had a back, drop that now-replaced image file.
    if front.back_crop_path:
        cropping.delete_crop(front.back_crop_path)
    front.back_crop_path = back.crop_path
    front.back_identification_json = back.identification_json
    # A MANUALLY-attached back is authoritative for the printed card NUMBER —
    # backs print it clearly while fronts often omit it, and the user is
    # explicitly asserting this pairing. Overwrite the number from the back (so a
    # stale number from a prior wrong match can't linger), and fill year/set/
    # parallel only where the front is missing them. Then always re-price so the
    # corrected identity drives the market match.
    if back.card_number:
        front.card_number = back.card_number
    pairing.enrich_front_from_back(front, back)  # fills year/set/parallel if missing
    pairing.remember_back_source(front, back, db)  # so the back's photo archives too
    if front.status == STATUS_PREVIEW:
        try:
            preview_card(front, db)
        except Exception:  # noqa: BLE001
            pass
    db.delete(back)
    db.commit()
    return front


def _frontness(c: Card) -> tuple:
    """How 'front-like' a card is, for deciding which of two cards is the front
    when the user pairs them. Higher wins: already a front > got priced (fronts
    drive pricing) > higher id confidence."""
    return (
        1 if c.side == "front" else 0,
        1 if c.estimated_price is not None else 0,
        c.confidence or 0.0,
    )


@router.post("/{front_id}/attach-back/{back_id}", response_model=CardDetailOut)
def attach_back(front_id: int, back_id: int, db: Session = Depends(get_db)) -> Card:
    """Attach a known back to a known front (front/back roles already determined,
    e.g. from the collection's unmatched-backs view)."""
    front = db.get(Card, front_id)
    back = db.get(Card, back_id)
    if front is None or back is None:
        raise HTTPException(status_code=404, detail="Card not found")
    return _attach_back(front, back, db)


@router.post("/{a_id}/pair/{b_id}", response_model=CardDetailOut)
def pair_cards(a_id: int, b_id: int, db: Session = Depends(get_db)) -> Card:
    """Pair ANY two un-added cards as the two sides of one physical card. The
    user asserts the pairing; we decide which is the front (the more front-like
    of the two) and attach the other as its back — regardless of how the AI
    labelled their sides. Used by the upload page's manual matcher."""
    if a_id == b_id:
        raise HTTPException(status_code=422, detail="Pick two different cards")
    a = db.get(Card, a_id)
    b = db.get(Card, b_id)
    if a is None or b is None:
        raise HTTPException(status_code=404, detail="Card not found")
    front, back = (a, b) if _frontness(a) >= _frontness(b) else (b, a)
    return _attach_back(front, back, db)


def _read_field(audit: dict, key: str) -> str | None:
    """Pull a structured value back out of an identification audit's field_reads."""
    fr = (audit or {}).get("field_reads") or {}
    v = fr.get(key)
    val = v.get("value") if isinstance(v, dict) else None
    return val or None


@router.post("/{front_id}/detach-back", response_model=CardDetailOut)
def detach_back(front_id: int, db: Session = Depends(get_db)) -> Card:
    """Undo a front/back match: split the attached back off as its own standalone
    back card again (so it returns to the unmatched section to be re-paired), and
    re-price the front without it."""
    front = db.get(Card, front_id)
    if front is None:
        raise HTTPException(status_code=404, detail="Card not found")
    if not front.back_crop_path:
        raise HTTPException(status_code=422, detail="This card has no back to detach")

    # Recreate the back as a standalone orphan, restoring its identity from the
    # stored back-identification audit so it shows meaningfully in the matcher.
    try:
        audit = json.loads(front.back_identification_json or "{}")
    except Exception:  # noqa: BLE001
        audit = {}
    back = Card(
        upload_id=front.upload_id,
        side="back",
        status=STATUS_PREVIEW,
        crop_path=front.back_crop_path,
        identification_json=front.back_identification_json,
        player=_read_field(audit, "player"),
        year=_read_field(audit, "year"),
        set_brand=_read_field(audit, "set_brand"),
        card_number=_read_field(audit, "card_number"),
        parallel=_read_field(audit, "parallel"),
        review_reason="card back — waiting for its matching front",
    )
    db.add(back)
    front.back_crop_path = None
    front.back_identification_json = None
    if front.status == STATUS_PREVIEW:
        try:
            preview_card(front, db)
        except Exception:  # noqa: BLE001
            pass
    db.commit()
    return front


@router.post("/{card_id}/price-from-url", response_model=CardDetailOut)
def price_card_from_url(
    card_id: int, req: PriceFromUrlRequest, db: Session = Depends(get_db)
) -> Card:
    """Manually price a card from a pasted SportsCardsPro product URL, for when
    the automatic search found the wrong card or no price. Scrapes that product's
    price, history, and image and pins the card to it."""
    card = db.get(Card, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    url = (req.url or "").strip()
    if not pricecharting.is_scp_url(url):
        raise HTTPException(
            status_code=422,
            detail="Please paste a sportscardspro.com (or pricecharting.com) product link.",
        )
    ok = price_from_url(card, db, url)
    if not ok:
        raise HTTPException(
            status_code=422,
            detail="Couldn't read price data from that link — check the URL.",
        )
    db.commit()
    return card


@router.post("/reprice")
def reprice_all(db: Session = Depends(get_db)) -> dict:
    """Force a fresh price re-fetch for every library card, bypassing the price
    cache. Backs the collection's "Refresh prices" button. Prices move slowly so
    the cache is effectively permanent; this is the on-demand way to update."""
    cards = list(
        db.scalars(
            select(Card).where(Card.side == "front", Card.status != STATUS_PREVIEW)
        ).all()
    )
    repriced = 0
    for card in cards:
        try:
            for comp in list(card.comps):
                db.delete(comp)
            db.flush()
            price_card(card, db, refresh=True)
            repriced += 1
        except Exception:  # noqa: BLE001
            logger.exception("reprice failed for card %s", card.id)
    db.commit()
    return {"repriced": repriced, "total": len(cards)}


@router.get("/{card_id}/back-crop")
def get_card_back_crop(card_id: int, db: Session = Depends(get_db)) -> FileResponse:
    """The matched back-of-card image, if one was paired to this card."""
    card = db.get(Card, card_id)
    if card is None or not card.back_crop_path:
        raise HTTPException(status_code=404, detail="No back image for this card")
    path = Path(card.back_crop_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Back crop file missing")
    stat = path.stat()
    etag = f'"{card_id}-back-{int(stat.st_mtime)}"'
    return FileResponse(
        path,
        headers={
            "Cache-Control": "no-cache",
            "ETag": etag,
        },
    )
