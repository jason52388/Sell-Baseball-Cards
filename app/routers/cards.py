"""Read endpoints for the card repository + per-card transparency detail."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.db import get_db
from app.models import (
    ITEM_WAITING,
    JOB_REPRICE,
    LISTING_SOLD,
    STATUS_BELOW_THRESHOLD,
    STATUS_DELETED,
    STATUS_LIST_FAILED,
    STATUS_LISTED,
    STATUS_NEEDS_REVIEW,
    STATUS_PREVIEW,
    STATUS_PRICED,
    Card,
    IdentificationCorrection,
    ImageUpload,
    Job,
    JobItem,
)
from app.routers.upload import _apply_detection
from app.schemas import (
    CardDetailOut,
    CardOut,
    CardUpdateRequest,
    ManualCardRequest,
    PriceFromUrlRequest,
    PromoteRequest,
)
from app.services import (
    cropping,
    dedupe,
    jobs,
    pairing,
    photo_archive,
    pricecharting,
    trash,
    vision,
)
from app.services.ebay import orders
from app.services.ebay.listing_common import base_list_price, listing_price_floor
from app.services.pricing import (
    finalize_card,
    price_card,
    price_from_url,
    preview_card,
    reprice_after_pairing,
)

logger = logging.getLogger("cards")
router = APIRouter(prefix="/api/cards", tags=["cards"])

# A phone photo is a few MB; well beyond that is not a card scan.
_MAX_PHOTO_BYTES = 25 * 1024 * 1024


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


def _card_or_404(db: Session, card_id: int, *, allow_deleted: bool = True) -> Card:
    card = db.get(Card, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    if not allow_deleted and card.status == STATUS_DELETED:
        raise HTTPException(status_code=409, detail="This card is deleted; restore it first")
    return card


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
        subset=(req.subset or "").strip() or None,
        team=(req.team or "").strip() or None,
        rookie=bool(req.rookie),
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


def _upload_label(up: ImageUpload) -> str:
    return photo_archive.source_photo_label(up.batch_tag, up.uploaded_at, up.filename)


def _back_source(card: Card) -> dict:
    try:
        audit = json.loads(card.back_identification_json or "{}")
    except Exception:  # noqa: BLE001
        return {}
    return audit if isinstance(audit, dict) else {}


def _archive_uploads(db: Session, upload_ids: set[int], loose: list[str] | None = None) -> int:
    """Move each source photo into the collection folder once NONE of its
    cards is still waiting in the upload queue. A photo of nine cards is moved
    when the last of them is added (or discarded), not when the first one is,
    and is named neutrally (batch tag or date + original name).

    `loose` are stored names of back photos recorded before back audits kept
    their upload id; they are moved as is. Best-effort: never raises."""
    moved = 0
    for uid in sorted(i for i in upload_ids if i):
        try:
            up = db.get(ImageUpload, uid)
            if up is None:
                continue
            waiting = db.scalar(
                select(func.count()).select_from(Card).where(
                    Card.upload_id == uid, Card.side == "front",
                    Card.status == STATUS_PREVIEW,
                )
            )
            if waiting:
                continue
            if photo_archive.archive_source_photo(up.stored_name or up.filename, _upload_label(up)):
                moved += 1
        except Exception:  # noqa: BLE001
            logger.exception("archiving the source photo of upload %s failed", uid)
    for name in loose or []:
        try:
            if photo_archive.archive_source_photo(name, Path(name).stem):
                moved += 1
        except Exception:  # noqa: BLE001
            logger.exception("archiving back photo %s failed", name)
    return moved


def _source_uploads(card: Card) -> tuple[set[int], list[str]]:
    """The card's own upload id plus its paired back's (or, for a back
    recorded before audits kept the id, the back photo's stored name)."""
    ids = {card.upload_id} if card.upload_id else set()
    loose: list[str] = []
    audit = _back_source(card)
    if audit.get("_upload_id"):
        ids.add(int(audit["_upload_id"]))
    elif audit.get("_stored_name") or audit.get("_source_filename"):
        loose.append(audit.get("_stored_name") or audit["_source_filename"])
    return ids, loose


@router.post("/promote")
def promote_cards(req: PromoteRequest, db: Session = Depends(get_db)) -> dict:
    """Add previewed cards to the repository. Each card runs the normal safeguard
    gating + status routing (priced / needs_review / below_threshold), reusing the
    estimate already computed at preview time.

    Returns {"added": [CardOut...], "skipped": [{"id", "reason"}...]}: only
    cards whose status actually changed are in `added`. Source photos are
    archived once no card from them is still in the queue."""
    settings = get_settings()
    added: list[Card] = []
    skipped: list[dict] = []
    upload_ids: set[int] = set()
    loose: list[str] = []
    crops_to_archive: list[tuple[str | None, str, str]] = []
    for card_id in req.card_ids:
        card = db.get(Card, card_id)
        if card is None:
            skipped.append({"id": card_id, "reason": "not found"})
            continue
        if card.status == STATUS_DELETED:
            skipped.append({"id": card_id, "reason": "deleted (restore it first)"})
            continue
        if card.status != STATUS_PREVIEW:
            skipped.append({"id": card_id, "reason": f"already in the collection ({card.status})"})
            continue
        if card.side != "front":
            skipped.append({"id": card_id, "reason": "a card back; pair it with its front instead"})
            continue
        finalize_card(card, settings)
        desc = _card_description(card)
        ids, extra = _source_uploads(card)
        upload_ids |= ids
        loose += extra
        # Copy crop images (front + back) into the collection folder.
        if card.crop_path:
            crops_to_archive.append((card.crop_path, desc, "front"))
        if card.back_crop_path:
            crops_to_archive.append((card.back_crop_path, desc, "back"))
        added.append(card)
    db.commit()
    _archive_uploads(db, upload_ids, loose)
    if crops_to_archive:
        try:
            photo_archive.archive_crop_files(crops_to_archive)
        except Exception:  # noqa: BLE001
            logger.exception("crop-photo archive step failed")
    try:
        photo_archive.backup_database()
    except Exception:  # noqa: BLE001
        logger.exception("database backup step failed")
    return {
        "added": [CardOut.model_validate(c).model_dump() for c in added],
        "skipped": skipped,
    }


@router.post("/{card_id}/reanalyze", response_model=CardDetailOut)
def reanalyze_card(card_id: int, db: Session = Depends(get_db)) -> Card:
    """Re-run identification with the strongest available model (the Claude
    CLI or Claude API when configured, else high-quality Gemini), then re-price.

    A paired card's front and back go in the same request. A field the back
    supplied is kept unless the new read is more confident in a different
    value, and a field the new read leaves empty keeps its old value. Works on
    previews and library cards; a card with a published eBay listing is
    refused (409), since its listing already carries its identity and price.
    A preview stays a preview; a library card is re-priced in place
    (reprice_after_pairing), never moved back to preview."""
    card = _card_or_404(db, card_id, allow_deleted=False)
    if card.is_listed or card.status == STATUS_LISTED:
        raise HTTPException(
            status_code=409,
            detail="This card is listed on eBay; end the listing before re-analyzing.",
        )
    if not card.crop_path or not Path(card.crop_path).exists():
        raise HTTPException(
            status_code=422,
            detail="No crop available to re-analyze. Add the card manually instead.",
        )

    try:
        crop_bytes = cropping.read_crop_bytes(card.crop_path)
        back_bytes = None
        if card.back_crop_path and Path(card.back_crop_path).exists():
            back_bytes = cropping.read_crop_bytes(card.back_crop_path)
        det, label = vision.reidentify_strongest(crop_bytes, back_bytes=back_bytes)
    except vision.MissingVisionKeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("re-analysis failed for card %s", card.id)
        raise HTTPException(status_code=502, detail=f"Re-analysis failed: {exc}")
    if det is None:
        raise HTTPException(
            status_code=422, detail="Re-analysis could not read a card in the crop."
        )

    # Apply the fresh identity (keep the existing crop/bbox and side), then put
    # back what the new read must not wipe.
    lent = pairing.back_supplied_fields(card)
    before = {f: getattr(card, f) for f in _REANALYSIS_KEEP_FIELDS}
    bbox, side = card.bbox_json, card.side
    _apply_detection(card, det)
    card.bbox_json, card.side = bbox, side
    kept: list[str] = []
    for field, old in before.items():
        new = getattr(card, field)
        if old and not new:
            setattr(card, field, old)  # the new read saw nothing: keep the old value
            continue
        if field in lent and old and new != old:
            read = det.field_reads.get(field)
            new_conf = read.confidence if read and read.value else det.confidence
            if new_conf <= lent[field]:
                setattr(card, field, old)
                kept.append(field)
    try:
        audit = json.loads(card.identification_json or "{}")
    except Exception:  # noqa: BLE001
        audit = {}
    if isinstance(audit, dict):
        audit["reanalysis"] = {
            "model": label,
            "with_back": back_bytes is not None,
            "confidence": det.confidence,
            "field_reads": {k: v.model_dump() for k, v in det.field_reads.items()},
            "kept_back_fields": kept,
        }
        card.identification_json = json.dumps(audit)

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
    db.flush()
    if card.status == STATUS_PREVIEW:
        preview_card(card, db)
    else:
        reprice_after_pairing(card, db)
    db.commit()
    return card


# Identity fields a re-analysis may not blank out (see reanalyze_card).
_REANALYSIS_KEEP_FIELDS = (
    "player", "year", "sport", "set_brand", "card_number", "parallel", "subset",
    "team", "rookie", "serial_number", "condition",
)


def _deleted_payload(card: Card, message: str | None = None) -> dict:
    until = trash.restore_until(card)
    return {
        "id": card.id,
        "status": card.status,
        "restore_until": until.isoformat() if until else None,
        "message": message,
    }


@router.delete("/{card_id}")
def discard_card(
    card_id: int,
    confirm: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> dict:
    """Delete a card: the upload "Discard" action and the library "Delete".

    A soft delete: the card is hidden and can be restored for 7 days
    (POST /api/cards/{id}/restore), then purged with its files on a later
    startup. A card with a LIVE eBay listing has that listing ended first; if
    eBay refuses, nothing is deleted (502 with eBay's message). A SOLD card
    needs confirm=true, since its record is the sale's history."""
    card = db.get(Card, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    if card.status == STATUS_DELETED:
        return _deleted_payload(card, "already deleted")
    state = orders.listing_state(card)
    if state == orders.STATE_SOLD and not confirm:
        raise HTTPException(
            status_code=409,
            detail="This card sold on eBay; its record is the sale's history. "
                   "Delete it anyway with confirm=true.",
        )
    message = None
    if state == orders.STATE_LIVE:
        try:
            result = orders.end_listing_for_card(db, card)
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            logger.exception("ending the listing of card %s failed", card_id)
            raise HTTPException(
                status_code=502,
                detail=f"eBay would not end the listing, so the card was not deleted: {exc}",
            )
        message = result.get("message")
    was_library = card.status != STATUS_PREVIEW
    trash.soft_delete(card)
    db.commit()
    # The last queued card of a photo leaving the queue lets its photo archive,
    # but only if some card from it made it into the collection.
    if card.upload_id and not was_library:
        has_library = db.scalar(
            select(func.count()).select_from(Card).where(
                Card.upload_id == card.upload_id,
                Card.status.notin_((STATUS_PREVIEW, STATUS_DELETED)),
            )
        )
        if has_library:
            _archive_uploads(db, {card.upload_id})
    return _deleted_payload(card, message)


@router.post("/{card_id}/restore", response_model=CardOut)
def restore_card(card_id: int, db: Session = Depends(get_db)) -> Card:
    """Undo a delete within 7 days. The card returns to the status it had."""
    card = db.get(Card, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    if card.status != STATUS_DELETED:
        raise HTTPException(status_code=409, detail="This card is not deleted")
    try:
        trash.restore(card)
    except trash.RestoreExpired as exc:
        raise HTTPException(status_code=410, detail=str(exc))
    db.commit()
    return card


def _library_cards(db: Session) -> list[Card]:
    """Every card in the collection (fronts, not queued, not deleted)."""
    return list(db.scalars(
        select(Card)
        .options(selectinload(Card.listings))
        .where(Card.side == "front", Card.status.notin_((STATUS_PREVIEW, STATUS_DELETED)))
    ).all())


@router.get("/stats")
def collection_stats(db: Session = Depends(get_db)) -> dict:
    """Collection-wide KPIs for the top of the My Collection page.

    List values use the same rule as the listing endpoints
    (listing_common.suggested_list_price: sold basis x PRICE_MARKUP, asking
    basis x EBAY_ASK_UNDERCUT, never below the floor, rounded to .99)."""
    s = get_settings()
    cards = _library_cards(db)
    priced = [c for c in cards if c.estimated_price is not None]
    floor = listing_price_floor(s)
    state = {c.id: orders.listing_state(c) for c in cards}

    def max_value(c: Card) -> float:
        return c.sold_max_estimate or c.estimated_price or 0

    def sell_expense(price: float) -> float:
        # Fee on the sale price + per-order fee + shipping supplies.
        return price * s.ebay_fee_pct + s.ebay_per_order_fee + s.supplies_cost_per_card

    def basis(c: Card) -> str:
        b = (c.price_basis or "").lower()
        return "sold" if b == "sold" else "asking" if b == "active" else "other"

    def live_price(c: Card) -> float:
        row = orders.live_listing(c)
        return (row.list_price if row and row.list_price else c.suggested_list_price) or 0.0

    by_basis = {k: [c for c in priced if basis(c) == k] for k in ("sold", "asking", "other")}
    live = [c for c in cards if state[c.id] == orders.STATE_LIVE]
    unsold_priced = [c for c in priced if state[c.id] != orders.STATE_SOLD]

    # Sales: a lot's rows each carry the whole lot's price, so count each
    # order once for money and each card once for the card count.
    month_start = datetime.now(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0, tzinfo=None
    )
    sales: dict[str, tuple[float, datetime | None]] = {}
    sold_cards = 0
    sold_cards_month = 0
    for c in cards:
        rows = [r for r in c.listings if r.status == LISTING_SOLD]
        if not rows:
            continue
        sold_cards += 1
        row = rows[0]
        when = row.sold_at.replace(tzinfo=None) if row.sold_at else None
        if when and when >= month_start:
            sold_cards_month += 1
        key = row.order_id or row.offer_id or f"row-{row.id}"
        sales.setdefault(key, (row.sold_price or 0.0, when))
    sold_total = sum(p for p, _ in sales.values())
    sold_month_total = sum(p for p, when in sales.values() if when and when >= month_start)

    under_floor = [
        c for c in unsold_priced
        if (b := base_list_price(c, s)) is not None and b < floor
    ]
    unmatched_backs = db.scalar(
        select(func.count()).select_from(Card).where(
            Card.side == "back", Card.status != STATUS_DELETED
        )
    ) or 0
    queued = db.scalar(
        select(func.count()).select_from(Card).where(
            Card.side == "front", Card.status == STATUS_PREVIEW
        )
    ) or 0
    in_trash = db.scalar(
        select(func.count()).select_from(Card).where(Card.status == STATUS_DELETED)
    ) or 0

    return {
        "card_count": len(cards),
        "priced_count": len(priced),
        "listed_count": len(live),
        "total_value": round(sum(c.estimated_price for c in priced), 2),
        "total_max_value": round(sum(max_value(c) for c in priced), 2),
        # Projected selling costs if every priced card sold at its MAX value.
        "selling_expenses": round(sum(sell_expense(max_value(c)) for c in priced), 2),
        "list_value_total": round(sum(c.suggested_list_price or 0 for c in unsold_priced), 2),
        "value_from_sold": round(sum(c.estimated_price for c in by_basis["sold"]), 2),
        "value_from_sold_count": len(by_basis["sold"]),
        "value_from_asking": round(sum(c.estimated_price for c in by_basis["asking"]), 2),
        "value_from_asking_count": len(by_basis["asking"]),
        "value_from_other": round(sum(c.estimated_price for c in by_basis["other"]), 2),
        "value_from_other_count": len(by_basis["other"]),
        "needs_review_count": sum(1 for c in cards if c.status == STATUS_NEEDS_REVIEW),
        "ready_to_list_count": sum(
            1 for c in cards
            if c.status == STATUS_PRICED and state[c.id] in (orders.STATE_NONE, orders.STATE_ENDED)
        ),
        "below_threshold_count": sum(1 for c in cards if c.status == STATUS_BELOW_THRESHOLD),
        "under_floor_count": len(under_floor),
        "price_floor": floor,
        "live_count": len(live),
        "live_value": round(sum(live_price(c) for c in live), 2),
        "active_listings_value": round(sum(live_price(c) for c in live), 2),
        "sold_count": sold_cards,
        "sold_total": round(sold_total, 2),
        "sold_this_month_count": sold_cards_month,
        "sold_this_month_total": round(sold_month_total, 2),
        "psa10_count": sum(1 for c in cards if c.psa10_candidate),
        "duplicates_count": len(dedupe.find_duplicates(cards)),
        "unmatched_backs_count": unmatched_backs,
        "queued_count": queued,
        "deleted_count": in_trash,
    }


@router.get("/duplicates")
def list_duplicates(db: Session = Depends(get_db)) -> dict:
    """Library cards that look like the same physical card.

    Declared ABOVE /{card_id} so the path isn't parsed as a card id.
    """
    # Listings eager-loaded: CardOut reads them (is_listed, listing_state).
    cards = _library_cards(db)
    groups = dedupe.find_duplicates(cards)
    return {
        "groups": [
            {
                "tier": g.tier,
                "label": g.label,
                "reason": g.reason,
                "cards": [CardOut.model_validate(c).model_dump() for c in g.cards],
            }
            for g in groups
        ]
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
        return list(db.scalars(
            stmt.where(Card.side == "back", Card.status != STATUS_DELETED)
        ).all())
    if status == STATUS_DELETED:
        # The trash: everything deleted (fronts and backs), restorable for 7 days.
        return list(db.scalars(stmt.where(Card.status == STATUS_DELETED)).all())
    # The collection shows fronts only; un-matched "back" rows stay hidden until
    # a matching front absorbs them (or are managed in the unmatched-backs view).
    stmt = stmt.where(Card.side == "front")
    if status:
        stmt = stmt.where(Card.status == status)
    else:
        # Default views never include un-added previews or deleted cards.
        stmt = stmt.where(Card.status.notin_((STATUS_PREVIEW, STATUS_DELETED)))
    return list(db.scalars(stmt).all())


@router.get("/{card_id}", response_model=CardDetailOut)
def get_card(card_id: int, db: Session = Depends(get_db)) -> Card:
    card = db.get(Card, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    return card


# Identity fields whose hand edits feed the corrections golden set.
_CORRECTION_FIELDS = (
    "player", "year", "sport", "set_brand", "card_number", "parallel", "subset",
    "team", "rookie", "serial_number",
)


def _record_corrections(card: Card, before: dict, db: Session) -> None:
    """Store one IdentificationCorrection per identity field the edit changed:
    the model's own read of it (from the card's identification audit), the
    value before the edit, and the final value."""
    try:
        audit = json.loads(card.identification_json or "{}")
    except Exception:  # noqa: BLE001
        audit = {}
    reads = audit.get("field_reads") if isinstance(audit, dict) else None
    reads = reads if isinstance(reads, dict) else {}

    def text(v):
        return None if v is None or v == "" else str(v)

    for field, old in before.items():
        new = getattr(card, field)
        if text(old) == text(new):
            continue
        read = reads.get(field)
        db.add(IdentificationCorrection(
            card_id=card.id, field=field,
            model_value=text(read.get("value")) if isinstance(read, dict) else None,
            previous_value=text(old), final_value=text(new),
            crop_path=card.crop_path, back_crop_path=card.back_crop_path,
        ))


@router.patch("/{card_id}", response_model=CardDetailOut)
def update_card(
    card_id: int, req: CardUpdateRequest, db: Session = Depends(get_db)
) -> Card:
    """Update editable metadata fields on a card. Only fields explicitly sent
    (non-None) are applied. After saving, re-prices the card from the new identity."""
    card = db.get(Card, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    if card.status == STATUS_DELETED:
        raise HTTPException(status_code=409, detail="This card is deleted; restore it first")
    before = {f: getattr(card, f) for f in _CORRECTION_FIELDS}
    changed = False
    for field in (
        "player", "year", "sport", "set_brand", "card_number",
        "parallel", "subset", "team", "serial_number", "condition",
    ):
        val = getattr(req, field)
        if val is not None:
            cleaned = val.strip() if isinstance(val, str) else val
            if field == "sport":
                cleaned = cleaned.lower() if cleaned else None
            setattr(card, field, cleaned or None)
            changed = True
    identity_edited = changed
    for field in ("psa10_candidate", "anomaly_flag", "rookie"):
        val = getattr(req, field)
        if val is not None:
            setattr(card, field, val)
            changed = True
    _record_corrections(card, before, db)
    if identity_edited:
        # Editing the identity by hand IS the fix for a low-confidence read, so
        # trust it — otherwise the confidence gate blocks the re-price and the
        # card is stuck in needs_review no matter what the user corrects.
        card.confidence = 1.0
    if changed:
        # Save the edit first, so the comp fetch below never runs inside it.
        db.commit()
        # A queued card stays queued: only Add puts it in the library, and Add
        # is also what archives its photos. A library card (needs_review
        # included) is re-priced and re-routed but stays in the library. A
        # card on eBay keeps its listed price: only the identity is saved.
        if card.status == STATUS_PREVIEW:
            preview_card(card, db, refresh=True, commit_after_fetch=True)
        elif _on_ebay(card):
            pass
        else:
            price_card(card, db, refresh=True, commit_after_fetch=True)
    db.commit()
    return card


def _on_ebay(card: Card) -> bool:
    """Listed now or sold: its price is the listing's, never re-routed."""
    return card.status in (STATUS_LISTED, STATUS_LIST_FAILED) or orders.listing_state(card) in (
        orders.STATE_LIVE, orders.STATE_SOLD
    )


@router.post("/{card_id}/replace-photo", response_model=CardDetailOut)
def replace_photo(
    card_id: int,
    side: str = Query(default="front"),
    db: Session = Depends(get_db),
    file: UploadFile = File(...),
) -> Card:
    """Replace the front or back photo for a card."""
    card = _card_or_404(db, card_id, allow_deleted=False)
    if side not in ("front", "back"):
        raise HTTPException(status_code=422, detail="side must be 'front' or 'back'")
    content = file.file.read(_MAX_PHOTO_BYTES + 1)
    if not content:
        raise HTTPException(status_code=422, detail="Empty file")
    if len(content) > _MAX_PHOTO_BYTES:
        raise HTTPException(
            status_code=422,
            detail=f"Photo is larger than {_MAX_PHOTO_BYTES // (1024 * 1024)} MB",
        )
    # The crops folder is served publicly at /crops, so never store bytes we
    # haven't proved are an image, and never take the extension from the client.
    try:
        new_path = cropping.save_replacement_photo(content, card_id)
    except cropping.NotAnImageError:
        raise HTTPException(status_code=422, detail="That file is not an image")
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


def _guard_consumed(card: Card, confirm: bool) -> None:
    """Refuse to merge a card into another as its back when that would lose
    something: a card on eBay (its Listing rows would go with it), a card that
    has its own back attached, or (without confirm=true) a card already in the
    collection."""
    if card.status == STATUS_DELETED:
        raise HTTPException(status_code=409, detail=f"Card #{card.id} is deleted; restore it first")
    state = orders.listing_state(card)
    if state in (orders.STATE_LIVE, orders.STATE_SOLD):
        word = "is listed" if state == orders.STATE_LIVE else "sold"
        raise HTTPException(
            status_code=409,
            detail=f"Card #{card.id} {word} on eBay, so it can't be merged into another card as its back.",
        )
    if card.back_crop_path:
        raise HTTPException(
            status_code=409,
            detail=f"Card #{card.id} has its own back attached; unmatch that first.",
        )
    if card.side == "front" and card.status != STATUS_PREVIEW and not confirm:
        raise HTTPException(
            status_code=409,
            detail=f"Card #{card.id} is in your collection. Using it as a back removes it "
                   "from the collection (its price and comps go with it). Repeat with confirm=true.",
        )


@router.post("/{card_id}/mark-back")
def mark_as_back(
    card_id: int,
    confirm: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> dict:
    """Reclassify a card the AI mislabeled as a front into a BACK. It leaves the
    collection (which shows fronts only) and tries to pair to its matching front;
    if none is found it becomes an un-matched back to attach manually.

    Refused (409) for a card on eBay or with its own back; a library card needs
    confirm=true."""
    card = db.get(Card, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    _guard_consumed(card, confirm)
    card.side = "back"
    card.status = STATUS_PREVIEW  # backs aren't part of the priced collection
    front = pairing.try_pair(card, db)  # as a back, attach to a matching front
    merged_into = front.id if front is not None else None
    db.commit()
    if front is not None:
        try:
            reprice_after_pairing(front, db, commit_after_fetch=True)
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("re-price after mark-back pairing failed for card %s", front.id)
        db.commit()
    return {"merged_into": merged_into}


def _read_field(audit: dict, key: str) -> str | None:
    """Pull a structured value back out of an identification audit's field_reads."""
    fr = (audit or {}).get("field_reads") or {}
    v = fr.get(key)
    val = v.get("value") if isinstance(v, dict) else None
    return val or None


def _split_off_back(front: Card, db: Session) -> Card:
    """Turn the front's attached back into its own orphan back card again,
    with the back's own upload, photo time and batch restored from the audit
    (so it can re-pair by timestamp and archives its own photo), and undo the
    identity the back lent the front. Caller re-prices the front and commits."""
    try:
        audit = json.loads(front.back_identification_json or "{}")
    except Exception:  # noqa: BLE001
        audit = {}
    if not isinstance(audit, dict):
        audit = {}
    upload_id = front.upload_id
    if audit.get("_upload_id") and db.get(ImageUpload, int(audit["_upload_id"])) is not None:
        upload_id = int(audit["_upload_id"])
    taken = None
    if audit.get("_photo_taken_at"):
        try:
            taken = datetime.fromisoformat(audit["_photo_taken_at"])
        except (TypeError, ValueError):
            taken = None
    conf = audit.get("confidence")
    back = Card(
        upload_id=upload_id,
        side="back",
        status=STATUS_PREVIEW,
        crop_path=front.back_crop_path,
        identification_json=front.back_identification_json,
        photo_taken_at=taken,
        batch_tag=audit.get("_batch_tag") or None,
        confidence=conf if isinstance(conf, (int, float)) else None,
        player=_read_field(audit, "player"),
        year=_read_field(audit, "year"),
        sport=_read_field(audit, "sport"),
        set_brand=_read_field(audit, "set_brand"),
        card_number=_read_field(audit, "card_number"),
        parallel=_read_field(audit, "parallel"),
        subset=_read_field(audit, "subset"),
        team=_read_field(audit, "team"),
        review_reason="card back — waiting for its matching front",
    )
    db.add(back)
    back_audit_json = front.back_identification_json
    front.back_crop_path = None
    front.back_identification_json = None
    # Undo the identity the back overwrote, so a wrong match doesn't leave the
    # front permanently carrying another card's number/year/set.
    pairing.restore_pre_pair_identity(front, back_audit_json)
    db.flush()
    return back


def _attach_back(front: Card, back: Card, db: Session) -> Card:
    """Core attach: move `back`'s image onto `front`, enrich + re-price, delete
    the back row. Coerces sides so it works even when the AI mislabeled them.

    A front that already has a back keeps that image: the old back is split
    off as an orphan back first (as unmatch does), never deleted."""
    if front.back_crop_path:
        _split_off_back(front, db)
    front.side = "front"
    back.side = "back"
    front.back_crop_path = back.crop_path
    front.back_identification_json = back.identification_json
    # A MANUALLY-attached back is authoritative for the printed card NUMBER —
    # backs print it clearly while fronts often omit it, and the user is
    # explicitly asserting this pairing. Overwrite the number from the back (so a
    # stale number from a prior wrong match can't linger), and fill year/set/
    # parallel only where the front is missing them. Then always re-price so the
    # corrected identity drives the market match.
    pairing.remember_pre_pair_identity(front)  # so detach can undo a wrong match
    if back.card_number:
        front.card_number = back.card_number
    pairing.enrich_front_from_back(front, back)  # fills year/set/parallel if missing
    pairing.remember_back_source(front, back, db)  # so the back's photo archives too
    db.delete(back)
    db.commit()
    try:
        reprice_after_pairing(front, db, commit_after_fetch=True)
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("re-price after attaching a back failed for card %s", front.id)
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
def attach_back(
    front_id: int,
    back_id: int,
    confirm: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> Card:
    """Attach a known back to a known front (front/back roles already determined,
    e.g. from the collection's unmatched-backs view). The back card is merged
    away: refused for a card on eBay, and a library card needs confirm=true."""
    if front_id == back_id:
        raise HTTPException(status_code=422, detail="Pick two different cards")
    front = db.get(Card, front_id)
    back = db.get(Card, back_id)
    if front is None or back is None:
        raise HTTPException(status_code=404, detail="Card not found")
    if front.status == STATUS_DELETED:
        raise HTTPException(status_code=409, detail=f"Card #{front.id} is deleted; restore it first")
    _guard_consumed(back, confirm)
    return _attach_back(front, back, db)


@router.post("/{a_id}/pair/{b_id}", response_model=CardDetailOut)
def pair_cards(
    a_id: int,
    b_id: int,
    confirm: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> Card:
    """Pair ANY two cards as the two sides of one physical card. The user
    asserts the pairing; we decide which is the front (the more front-like of
    the two) and attach the other as its back — regardless of how the AI
    labelled their sides. Used by the upload page's manual matcher. The card
    used as the back is merged away (same guards as attach-back)."""
    if a_id == b_id:
        raise HTTPException(status_code=422, detail="Pick two different cards")
    a = db.get(Card, a_id)
    b = db.get(Card, b_id)
    if a is None or b is None:
        raise HTTPException(status_code=404, detail="Card not found")
    front, back = (a, b) if _frontness(a) >= _frontness(b) else (b, a)
    if front.status == STATUS_DELETED:
        raise HTTPException(status_code=409, detail=f"Card #{front.id} is deleted; restore it first")
    _guard_consumed(back, confirm)
    return _attach_back(front, back, db)


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
    _split_off_back(front, db)
    db.commit()
    try:
        reprice_after_pairing(front, db, commit_after_fetch=True)
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("re-price after detaching a back failed for card %s", front.id)
    db.commit()
    return front


@router.post("/{card_id}/price-from-url", response_model=CardDetailOut)
def price_card_from_url(
    card_id: int, req: PriceFromUrlRequest, db: Session = Depends(get_db)
) -> Card:
    """Manually price a card from a pasted SportsCardsPro product URL, for when
    the automatic search found the wrong card or no price. Scrapes that product's
    price, history, and image and pins the card to it."""
    card = _card_or_404(db, card_id, allow_deleted=False)
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


def _reprice_handler(db: Session, job: Job, item: JobItem, progress) -> None:
    """Background job: re-fetch one card's price, bypassing the cache."""
    item_id = item.id
    card = db.get(Card, item.card_id) if item.card_id else None
    if card is None or card.status == STATUS_DELETED:
        item.message = "skipped: the card was deleted"
        return
    jobs.set_card_ids(item, [card.id])
    if _on_ebay(card):
        item.message = "skipped: on eBay, its listed price stands"
        return
    if card.status == STATUS_PREVIEW:
        item.message = "skipped: still in the upload queue"
        return
    progress("Fetching prices")
    price_card(card, db, refresh=True, commit_after_fetch=True)
    db.commit()
    item = db.get(JobItem, item_id)
    if card.estimated_price is not None:
        item.message = f"${card.estimated_price:.2f} ({card.price_basis or 'estimate'})"
    else:
        item.message = card.review_reason or "no price found"


jobs.register(JOB_REPRICE, _reprice_handler)


@router.post("/reprice")
def reprice_all(db: Session = Depends(get_db)) -> dict:
    """Force a fresh price re-fetch for every library card, bypassing the price
    cache. Backs the collection's "Refresh prices" button.

    Runs as a background job (kind "reprice", one item per card, a commit per
    card); returns the job at once, like an upload. Poll GET /api/jobs/{id}.
    A refresh already running is returned instead of starting a second.
    Cards on eBay keep their listed price and are skipped."""
    running = db.scalars(
        select(Job).where(Job.kind == JOB_REPRICE, Job.finished_at.is_(None))
    ).first()
    if running is not None:
        return jobs.serialize(running)
    cards = list(db.scalars(
        select(Card)
        .where(Card.side == "front", Card.status.notin_((STATUS_PREVIEW, STATUS_DELETED)))
        .order_by(Card.id)
    ).all())
    job = jobs.new_job(db, JOB_REPRICE)
    for card in cards:
        jobs.add_item(db, job, f"#{card.id} {_card_description(card)}", card_id=card.id)
    if not cards:
        job.finished_at = jobs.now()
    db.commit()
    job_id = job.id
    jobs.kick()
    db.expire_all()
    return jobs.serialize(db.get(Job, job_id))


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
