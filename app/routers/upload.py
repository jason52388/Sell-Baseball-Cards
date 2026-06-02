"""Mass-upload endpoint: one or many images -> detect, crop, verify, price, store."""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Card, ImageUpload
from app.schemas import CardOut, DetectedCard, UploadFileResult, UploadResponse
from app.services import cropping, vision
from app.services.pricing import price_card

logger = logging.getLogger("upload")
router = APIRouter(prefix="/api", tags=["upload"])


def _apply_detection(card: Card, det: DetectedCard) -> None:
    card.player = det.player
    card.year = det.year
    card.set_brand = det.set_brand
    card.card_number = det.card_number
    card.parallel = det.parallel
    card.serial_number = det.serial_number
    card.condition = det.condition
    card.confidence = det.confidence
    card.bbox_json = json.dumps(det.bbox)
    card.grade_estimate = det.grade_estimate
    card.gem_mint_score = det.gem_mint_score
    card.psa10_candidate = bool(det.psa10_candidate)
    card.grading_notes = det.grading_notes
    card.anomaly_flag = bool(det.anomaly_flag)
    card.anomaly_notes = det.anomaly_notes


def _process_image(filename: str, image_bytes: bytes, db: Session) -> UploadFileResult:
    settings = get_settings()

    upload = ImageUpload(filename=filename)
    db.add(upload)
    db.flush()  # assign upload.id

    try:
        detections = vision.detect_cards(image_bytes)
    except Exception as exc:  # noqa: BLE001 — isolate per-file failures
        logger.exception("detection failed for %s", filename)
        upload.error = f"detection failed: {exc}"
        db.commit()
        return UploadFileResult(upload_id=upload.id, filename=filename, error=upload.error)

    upload.raw_vision_json = json.dumps([d.model_dump() for d in detections])
    upload.card_count = len(detections)

    out_cards: list[CardOut] = []
    for det in detections:
        card = Card(upload_id=upload.id)
        _apply_detection(card, det)
        db.add(card)
        db.flush()  # assign card.id for crop filename

        crop_path = None
        try:
            crop_path = cropping.crop_card(image_bytes, det.bbox, card.id)
        except Exception:  # noqa: BLE001
            logger.exception("crop failed for card %s", card.id)
        card.crop_path = crop_path

        # Optional second-pass identity verification.
        ident_audit = {
            "raw_text": det.raw_text,
            "field_reads": {k: v.model_dump() for k, v in det.field_reads.items()},
        }
        if settings.verify_identification and crop_path:
            try:
                crop_bytes = cropping.read_crop_bytes(crop_path)
                result = vision.verify_card(crop_bytes, det)
                ident_audit["verification"] = result.model_dump()
                if not result.agree:
                    # Disagreement lowers confidence -> safeguard flags it.
                    card.confidence = min(card.confidence or 0.0, 0.4)
            except Exception:  # noqa: BLE001
                logger.exception("verification failed for card %s", card.id)
        card.identification_json = json.dumps(ident_audit)

        # Price (real sold comps only) + safeguard gating + status routing.
        try:
            price_card(card, db)
        except Exception:  # noqa: BLE001
            logger.exception("pricing failed for card %s", card.id)
            card.status = "needs_review"
            card.review_reason = "pricing error"

        out_cards.append(CardOut.model_validate(card))

    db.commit()
    return UploadFileResult(
        upload_id=upload.id,
        filename=filename,
        card_count=upload.card_count,
        cards=out_cards,
    )


@router.post("/upload", response_model=UploadResponse)
async def upload(
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
) -> UploadResponse:
    results: list[UploadFileResult] = []
    for f in files:
        image_bytes = await f.read()
        results.append(_process_image(f.filename or "upload", image_bytes, db))
    return UploadResponse(results=results)
