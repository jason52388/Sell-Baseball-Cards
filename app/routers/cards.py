"""Read endpoints for the card repository + per-card transparency detail."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.db import get_db
from app.models import STATUS_PREVIEW, Card, ImageUpload
from app.routers.upload import _apply_detection
from app.schemas import CardDetailOut, CardOut, ManualCardRequest, PromoteRequest
from app.services import cropping, pairing, vision
from app.services.pricing import finalize_card, preview_card, price_card

logger = logging.getLogger("cards")
router = APIRouter(prefix="/api/cards", tags=["cards"])


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
    for card_id in req.card_ids:
        card = db.get(Card, card_id)
        if card is None:
            continue
        if card.status == STATUS_PREVIEW:
            finalize_card(card, settings)
        promoted.append(card)
    db.commit()
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


@router.post("/{front_id}/attach-back/{back_id}", response_model=CardDetailOut)
def attach_back(front_id: int, back_id: int, db: Session = Depends(get_db)) -> Card:
    """Manually attach an un-matched back scan to a front (for cases the
    automatic identity match missed). Moves the back's image onto the front and
    removes the standalone back row."""
    front = db.get(Card, front_id)
    back = db.get(Card, back_id)
    if front is None or back is None:
        raise HTTPException(status_code=404, detail="Card not found")
    if front.side != "front":
        raise HTTPException(status_code=422, detail="Target card is not a front")
    if back.side != "back":
        raise HTTPException(status_code=422, detail="Source card is not a back")
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
    if front.status == STATUS_PREVIEW:
        try:
            preview_card(front, db)
        except Exception:  # noqa: BLE001
            pass
    db.delete(back)
    db.commit()
    return front


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
