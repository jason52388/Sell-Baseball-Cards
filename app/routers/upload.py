"""Mass-upload endpoint: one or many images -> detect, crop, verify, price, store."""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import INBOX_DIR, get_settings
from app.db import get_db
from app.models import STATUS_PREVIEW, Card, ImageUpload
from app.schemas import CardOut, DetectedCard, UploadFileResult, UploadResponse
from app.services import cropping, exif, pairing, vision
from app.services.pricing import preview_card, reprice_after_pairing

logger = logging.getLogger("upload")
router = APIRouter(prefix="/api", tags=["upload"])


def _clean_tag(tag: str | None) -> str | None:
    """Normalize a user-supplied batch tag: trimmed, single-line, capped."""
    if not tag:
        return None
    cleaned = " ".join(tag.split())[:128].strip()
    return cleaned or None


def _safe_source_name(filename: str | None) -> str:
    """Strip any directory part from a client-supplied filename.

    This name is stored on the ImageUpload and later joined to the inbox path and
    moved during archival, so a name like "../../.env" would relocate a file from
    outside the inbox.
    """
    base = (filename or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1].strip()
    if base in ("", ".", ".."):
        return "upload"
    return base[:200]


def _safe_inbox_name(filename: str) -> str:
    """A collision-proof, path-safe filename for the inbox (keeps the extension)."""
    base = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." in base:
        stem, _, ext = base.rpartition(".")
    else:
        stem, ext = base, "jpg"
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem)[:80] or "photo"
    ext = re.sub(r"[^A-Za-z0-9]", "", ext)[:8].lower() or "jpg"
    return f"{stem}-{uuid.uuid4().hex[:8]}.{ext}"


# A real card fills a meaningful share of its frame; the vision model sometimes
# hallucinates a tiny extra "card" in a single-card photo (a sliver of table or
# sleeve edge). Reject detections whose box is too small to be a card, or which
# are near-zero-confidence with no identity read at all.
_MIN_CARD_BBOX_AREA = 0.03  # 3% of the image (even a 3x3 grid cell is ~11%)


def _is_phantom_detection(det: DetectedCard, *, from_grid: bool = False) -> bool:
    """Neither rule applies to grid cells: their boxes are produced by an even
    split (a 10x10 cell is 1% of the photo, well under the area floor), and an
    unreadable cell is meant to survive as a low-confidence preview the user can
    fix or discard."""
    if from_grid:
        return False
    bbox = det.bbox or []
    area = (bbox[2] * bbox[3]) if len(bbox) == 4 else 0.0
    if area < _MIN_CARD_BBOX_AREA:
        return True
    if (det.confidence or 0.0) < 0.2 and not det.player and not det.card_number:
        return True
    return False


def _apply_detection(card: Card, det: DetectedCard) -> None:
    card.player = det.player
    card.year = det.year
    card.sport = (det.sport or "").strip().lower() or None
    side = (det.side or "front").strip().lower()
    card.side = side if side in ("front", "back") else "front"
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


def _cards_from_detections(
    filename: str,
    image_bytes: bytes,
    detections: list[DetectedCard],
    db: Session,
    verify: bool,
    batch_tag: str | None = None,
    photo_taken_at: datetime | None = None,
    from_grid: bool = False,
) -> UploadFileResult:
    """Crop + price each detection as a review preview, regardless of where the
    detections came from (in-app vision, or ingested from an external Claude).

    `verify` runs the second-pass identity check (needs a vision API key); the
    ingest path passes False so it requires no key at all. `from_grid` marks
    detections produced by an even grid split, which are never phantoms.
    """
    upload = ImageUpload(filename=filename, batch_tag=batch_tag)
    db.add(upload)
    db.flush()  # assign upload.id

    upload.raw_vision_json = json.dumps([d.model_dump() for d in detections])
    upload.card_count = len(detections)

    real = [d for d in detections if not _is_phantom_detection(d, from_grid=from_grid)]
    # One card in the photo: crop it loosely. There is no neighbouring card the
    # margin could swallow, so a wide border is free, while a box that sits a few
    # pixels inside the card would otherwise shave off its edge.
    settings = get_settings()
    pad = settings.single_card_pad_pct if (len(real) == 1 and not from_grid) else None

    out_cards: list[CardOut] = []
    for det in detections:
        if _is_phantom_detection(det, from_grid=from_grid):
            logger.info("skipping phantom detection (bbox=%s conf=%s)", det.bbox, det.confidence)
            upload.card_count = max(0, (upload.card_count or 0) - 1)
            continue
        card = Card(upload_id=upload.id, batch_tag=batch_tag, photo_taken_at=photo_taken_at)
        _apply_detection(card, det)
        db.add(card)
        db.flush()  # assign card.id for crop filename

        crop_path = None
        try:
            crop_path = cropping.crop_card(image_bytes, det.bbox, card.id, pad=pad)
        except Exception:  # noqa: BLE001
            logger.exception("crop failed for card %s", card.id)
        card.crop_path = crop_path
        if crop_path:
            card.photo_quality = cropping.assess_quality(crop_path)

        ident_audit = {
            "raw_text": det.raw_text,
            "field_reads": {k: v.model_dump() for k, v in det.field_reads.items()},
        }

        # BACK of a card: don't price it; attach it to its matching front.
        if card.side == "back":
            card.identification_json = json.dumps(ident_audit)
            card.status = STATUS_PREVIEW
            front = pairing.try_pair(card, db)
            if front is not None:
                # The front may have gained this back's year/number — re-price it
                # so the sharper identity drives the market match. The front can
                # already be in the library (backs are often shot later), so this
                # must not send it back to preview.
                try:
                    reprice_after_pairing(front, db)
                except Exception:  # noqa: BLE001
                    logger.exception("re-price after back-pair failed for card %s", front.id)
                continue  # merged into a front — not its own card
            # No front yet: keep as a hidden orphan back (a later front absorbs it).
            card.review_reason = "card back — waiting for its matching front"
            continue

        # FRONT: optional second-pass identity verification, then price.
        if verify and crop_path:
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

        # Price for review only (real sold comps + reference photo); the card
        # stays in "preview" until the user explicitly adds it to the repository.
        try:
            preview_card(card, db)
        except Exception:  # noqa: BLE001
            logger.exception("pricing failed for card %s", card.id)
            card.status = "preview"
            card.review_reason = "pricing error"

        # Pull in a matching back that may have been uploaded earlier; if it
        # enriched the front's identity (added a year/number), re-price with the
        # sharper match.
        if pairing.try_pair(card, db) is not None:
            try:
                reprice_after_pairing(card, db)
            except Exception:  # noqa: BLE001
                logger.exception("re-price after absorbing back failed for card %s", card.id)
        out_cards.append(CardOut.model_validate(card))

    db.commit()
    return UploadFileResult(
        upload_id=upload.id,
        filename=filename,
        card_count=upload.card_count,
        cards=out_cards,
    )


def _detect_by_grid(
    image_bytes: bytes, rows: int, cols: int, filename: str
) -> list[DetectedCard]:
    """Split the photo into an even rows×cols grid and identify each cell on its
    own. Used when the cards are laid out in a neat grid — an even split crops far
    more reliably than AI-guessed boxes that drift or overlap.

    Every cell becomes a card (even an unreadable one, as a low-confidence preview
    the user can fix or discard), so the per-cell bbox always matches its crop.
    """
    detections: list[DetectedCard] = []
    for bbox, cell_bytes in cropping.grid_cells(image_bytes, rows, cols):
        try:
            det = vision.reidentify(cell_bytes) or DetectedCard()
        except vision.MissingVisionKeyError:
            raise
        except Exception:  # noqa: BLE001 — one bad cell shouldn't sink the grid
            logger.exception("grid cell identification failed for %s", filename)
            det = DetectedCard()
        det.bbox = bbox
        detections.append(det)
    return detections


def _process_image(
    filename: str,
    image_bytes: bytes,
    db: Session,
    grid: tuple[int, int] | None = None,
    batch_tag: str | None = None,
) -> UploadFileResult:
    """Upload path: identify cards with the in-app vision model, then preview.

    When `grid` is given, the photo is split into that many (rows, cols) equal
    cells and each is identified separately; otherwise the model auto-detects
    cards and their bounding boxes.
    """
    settings = get_settings()
    photo_taken_at = exif.extract_datetime(image_bytes)
    try:
        if grid:
            detections = _detect_by_grid(image_bytes, grid[0], grid[1], filename)
        else:
            detections = vision.detect_cards(image_bytes)
    except Exception as exc:  # noqa: BLE001 — isolate per-file failures
        logger.exception("detection failed for %s", filename)
        upload = ImageUpload(
            filename=filename, error=f"detection failed: {exc}", batch_tag=batch_tag
        )
        db.add(upload)
        db.commit()
        return UploadFileResult(upload_id=upload.id, filename=filename, error=upload.error)

    return _cards_from_detections(
        filename, image_bytes, detections, db,
        verify=settings.verify_identification, batch_tag=batch_tag,
        photo_taken_at=photo_taken_at, from_grid=grid is not None,
    )


@router.post("/upload", response_model=UploadResponse)
async def upload(
    files: list[UploadFile] = File(...),
    grid_rows: int = Form(default=0),
    grid_cols: int = Form(default=0),
    batch_tag: str = Form(default=""),
    db: Session = Depends(get_db),
) -> UploadResponse:
    # Optional even-grid split: when both dims are given, slice each photo into
    # rows×cols equal cells instead of auto-detecting boxes. Capped to keep a
    # stray value from producing a runaway number of crops.
    grid: tuple[int, int] | None = None
    if grid_rows > 0 and grid_cols > 0:
        grid = (min(grid_rows, 10), min(grid_cols, 10))

    tag = _clean_tag(batch_tag)
    results: list[UploadFileResult] = []
    for f in files:
        image_bytes = await f.read()
        # Detection, verification and pricing are blocking network + image work
        # that runs for minutes on a batch. Off the event loop, or the whole app
        # (collection page, crop images, eBay endpoints) stalls until it ends.
        results.append(
            await run_in_threadpool(
                _process_image,
                _safe_source_name(f.filename), image_bytes, db,
                grid=grid, batch_tag=tag,
            )
        )
    return UploadResponse(results=results)


@router.post("/queue")
async def queue_photos(
    files: list[UploadFile] = File(...),
    batch_tag: str = Form(default=""),
) -> dict:
    """Save dropped photos to the inbox folder for later identification by the
    Claude subscription loop (tools/ingest_folder.sh) — NO AI call here.

    This decouples intake (easy web drag-drop) from identification, so dropping
    photos never hits a vision API or its rate limits. They're identified when
    you run the ingest, and land as previews to review/add.

    A batch tag (if given) is written to a `<name>.tag` sidecar next to each
    photo so it survives the round-trip and ingest_folder.sh can forward it.
    """
    tag = _clean_tag(batch_tag)
    saved: list[str] = []
    for f in files:
        data = await f.read()
        if not data:
            continue
        name = _safe_inbox_name(f.filename or "photo")
        (INBOX_DIR / name).write_bytes(data)
        if tag:
            (INBOX_DIR / f"{name}.tag").write_text(tag, encoding="utf-8")
        saved.append(name)
    return {"queued": len(saved), "files": saved, "inbox": str(INBOX_DIR)}


@router.post("/ingest", response_model=UploadFileResult)
async def ingest(
    image: UploadFile = File(...),
    detections: str = Form(...),
    batch_tag: str = Form(default=""),
    db: Session = Depends(get_db),
) -> UploadFileResult:
    """Ingest cards that were identified OUTSIDE the app (e.g. by Claude Code
    reading a folder of photos on a subscription, with no API key here).

    Accepts the original image plus a JSON string of detections — either
    {"cards": [...]} or a bare list — matching the schema in
    app/prompts/card_detection.py. The server still crops and prices each card
    and lands it as a `preview` to review/add, exactly like a photo upload.
    """
    try:
        parsed = vision.parse_detection(detections)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"Invalid detections JSON: {exc}")
    if not parsed:
        raise HTTPException(status_code=422, detail="No cards found in detections JSON.")

    image_bytes = await image.read()
    photo_taken_at = exif.extract_datetime(image_bytes)
    # No vision API key needed: verify=False (the second-pass check would call the
    # vision model). Per-field confidence from the ingested JSON still gates review.
    # Cropping and pricing still block, so keep them off the event loop.
    return await run_in_threadpool(
        _cards_from_detections,
        _safe_source_name(image.filename), image_bytes, parsed, db,
        verify=False, batch_tag=_clean_tag(batch_tag),
        photo_taken_at=photo_taken_at,
    )
