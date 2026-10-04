"""Upload endpoints: photos -> saved originals + a background job that detects,
crops, pairs, verifies and prices each photo (see app/services/jobs.py)."""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import INBOX_DIR, get_settings
from app.db import get_db
from app.models import (
    ITEM_DONE,
    ITEM_FAILED,
    ITEM_WAITING,
    JOB_UPLOAD,
    STATUS_DELETED,
    STATUS_PREVIEW,
    Card,
    ImageUpload,
    Job,
    JobItem,
)
from app.schemas import CardOut, DetectedCard, UploadFileResult
from app.services import cropping, exif, images, jobs, pairing, photo_archive, vision
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
    card.subset = det.subset
    card.team = det.team
    card.rookie = bool(det.rookie)
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


# Fields the verifier may correct. Each correction must name its evidence
# (reason) and be at least VERIFY_CORRECTION_MIN_CONFIDENCE sure to be applied.
_VERIFIABLE_FIELDS = (
    "player", "year", "set_brand", "card_number", "parallel", "subset", "team",
    "serial_number",
)
# A verifier disagreement that could not be resolved caps confidence here, so
# the pricing safeguard sends the card to review.
_DISAGREE_CONFIDENCE = 0.4


def _proposed_identity(card: Card) -> DetectedCard:
    return DetectedCard(
        player=card.player, year=card.year, set_brand=card.set_brand,
        card_number=card.card_number, parallel=card.parallel, subset=card.subset,
        team=card.team, rookie=bool(card.rookie), serial_number=card.serial_number,
    )


def _verify_front(card: Card, ident_audit: dict) -> None:
    """Second-pass check of a front's identity (and its back, when one is
    already paired). Records the outcome in `ident_audit["verification"]`.

    - agree=None (could not confirm, e.g. no year on the front) changes nothing.
    - A correction with a reason and a confident value is applied; any other
      correction is only flagged.
    - Disagreement that is not fully resolved by applied corrections caps the
      confidence at _DISAGREE_CONFIDENCE so the card lands in review.
    - A failed call is recorded as {"error": ...} so it is visible.
    """
    settings = get_settings()
    try:
        crop_bytes = cropping.read_crop_bytes(card.crop_path)
        back_bytes = None
        if card.back_crop_path:
            try:
                back_bytes = cropping.read_crop_bytes(card.back_crop_path)
            except OSError:
                back_bytes = None
        proposed = _proposed_identity(card)
        if back_bytes:
            result = vision.verify_card(crop_bytes, proposed, back_bytes=back_bytes)
        else:
            result = vision.verify_card(crop_bytes, proposed)
    except vision.MissingVisionKeyError as exc:
        logger.warning("verification skipped for card %s: %s", card.id, exc)
        ident_audit["verification"] = {"error": f"skipped: {exc}"}
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("verification failed for card %s", card.id)
        ident_audit["verification"] = {"error": str(exc)[:500]}
        return

    record = result.model_dump()
    applied: dict[str, dict] = {}
    flagged: list[str] = []
    min_conf = settings.verify_correction_min_confidence
    for field, corr in result.corrections.items():
        if field not in _VERIFIABLE_FIELDS:
            flagged.append(field)
            continue
        old = getattr(card, field, None)
        if corr.value is not None and str(corr.value) == str(old or ""):
            continue  # "correction" to the value it already has
        if corr.value and corr.reason and (corr.confidence or 0.0) >= min_conf:
            setattr(card, field, corr.value)
            applied[field] = {"from": old, "to": corr.value}
        else:
            flagged.append(field)
    record["applied"] = applied
    record["flagged"] = flagged
    ident_audit["verification"] = record

    if result.agree is False or flagged:
        resolved = bool(applied) and not flagged
        if not resolved:
            card.confidence = min(card.confidence or 0.0, _DISAGREE_CONFIDENCE)
        else:
            lowest = min(result.corrections[f].confidence or 0.0 for f in applied)
            card.confidence = min(card.confidence or 0.0, lowest)




def _note(progress, step: str) -> None:
    if progress is not None:
        progress(step)


def _cards_from_detections(
    filename: str,
    image_bytes: bytes,
    detections: list[DetectedCard],
    db: Session,
    verify: bool,
    batch_tag: str | None = None,
    photo_taken_at: datetime | None = None,
    from_grid: bool = False,
    upload: ImageUpload | None = None,
    progress=None,
) -> UploadFileResult:
    """Crop + price each detection as a review preview, regardless of where the
    detections came from (in-app vision, or ingested from an external Claude).

    `verify` runs the second-pass identity check on each front (needs a vision
    provider; see _verify_front). `from_grid` marks detections produced by an
    even grid split, which are never phantoms. `upload` is the photo's
    ImageUpload when the caller already recorded it (background jobs, ingest);
    `progress(step)` reports each step and commits.

    Commits after every step, so the slow work (verification, comp fetches)
    never runs while this photo holds SQLite's write lock:
      1. cards + crops, 2. front/back pairing, 3. per front: verify, then price,
      4. re-price fronts elsewhere that gained one of this photo's backs.
    """
    if upload is None:
        upload = ImageUpload(filename=filename, batch_tag=batch_tag)
        db.add(upload)
        db.flush()  # assign upload.id
    upload.error = None
    upload.raw_vision_json = json.dumps([d.model_dump() for d in detections])

    real = [d for d in detections if not _is_phantom_detection(d, from_grid=from_grid)]
    for det in detections:
        if det not in real:
            logger.info("skipping phantom detection (bbox=%s conf=%s)", det.bbox, det.confidence)
    upload.card_count = len(real)
    # One card in the photo: crop it loosely. There is no neighbouring card the
    # margin could swallow, so a wide border is free, while a box that sits a few
    # pixels inside the card would otherwise shave off its edge.
    settings = get_settings()
    pad = settings.single_card_pad_pct if (len(real) == 1 and not from_grid) else None

    # --- 1. cards + crops ---------------------------------------------------
    if real:
        _note(progress, f"Cropping {len(real)} card{'s' if len(real) != 1 else ''}")
    new_cards: list[Card] = []
    for det in real:
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
        card.identification_json = json.dumps({
            "raw_text": det.raw_text,
            # This side's own overall read, kept apart from card.confidence
            # (which pairing and verification may later change).
            "confidence": det.confidence,
            "field_reads": {k: v.model_dump() for k, v in det.field_reads.items()},
        })
        card.status = STATUS_PREVIEW
        new_cards.append(card)
    db.commit()

    # --- 2. pairing ----------------------------------------------------------
    if new_cards:
        _note(progress, "Matching fronts and backs")
    fronts: list[Card] = []
    paired_elsewhere: list[Card] = []
    backs_waiting = 0
    new_ids = {c.id for c in new_cards}
    for card in new_cards:
        if card.side == "back":
            # BACK of a card: don't price it; attach it to its matching front.
            front = pairing.try_pair(card, db)
            if front is None:
                # No front yet: a hidden orphan back (a later front absorbs it).
                card.review_reason = "card back — waiting for its matching front"
                backs_waiting += 1
            elif front.id not in new_ids:
                # The front can already be in the library (backs are often shot
                # later); it is re-priced below without leaving the library.
                paired_elsewhere.append(front)
            continue
        # FRONT: pull in a matching back uploaded earlier. It may add the
        # year/number and lift the confidence, and the verifier can then check
        # the combined identity against both images.
        pairing.try_pair(card, db)
        fronts.append(card)
    db.commit()

    # --- 3. verify + price each front -----------------------------------------
    out_cards: list[CardOut] = []
    n = len(fronts)
    for i, card in enumerate(fronts, 1):
        if verify and card.crop_path:
            _note(progress, f"Verifying card {i} of {n}")
            ident_audit = json.loads(card.identification_json or "{}")
            _verify_front(card, ident_audit)
            card.identification_json = json.dumps(ident_audit)
            db.commit()
        # Price for review only (real sold comps + reference photo); the card
        # stays in "preview" until the user explicitly adds it to the repository.
        _note(progress, f"Pricing card {i} of {n}")
        try:
            preview_card(card, db, commit_after_fetch=True)
        except Exception:  # noqa: BLE001
            logger.exception("pricing failed for card %s", card.id)
            db.rollback()
            card = db.get(Card, card.id)
            card.status = STATUS_PREVIEW
            card.review_reason = "pricing error"
        db.commit()
        out_cards.append(CardOut.model_validate(card))

    # --- 4. fronts elsewhere that gained one of this photo's backs -------------
    for front in paired_elsewhere:
        _note(progress, f"Re-pricing card #{front.id} with its new back")
        try:
            reprice_after_pairing(front, db, commit_after_fetch=True)
        except Exception:  # noqa: BLE001
            logger.exception("re-price after back-pair failed for card %s", front.id)
            db.rollback()
        db.commit()

    db.commit()
    return UploadFileResult(
        upload_id=upload.id,
        filename=filename,
        card_count=upload.card_count,
        cards=out_cards,
        paired_into=[f.id for f in paired_elsewhere],
        backs_waiting=backs_waiting,
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
    upload: ImageUpload | None = None,
    progress=None,
    verify: bool | None = None,
) -> UploadFileResult:
    """Identify cards with the in-app vision model, then preview them.

    When `grid` is given, the photo is split into that many (rows, cols) equal
    cells and each is identified separately; otherwise the model auto-detects
    cards and their bounding boxes.
    """
    settings = get_settings()
    photo_taken_at = exif.extract_datetime(image_bytes)
    _note(progress, "Finding cards")
    try:
        if grid:
            detections = _detect_by_grid(image_bytes, grid[0], grid[1], filename)
        else:
            detections = vision.detect_cards(image_bytes)
    except Exception as exc:  # noqa: BLE001 — isolate per-file failures
        logger.exception("detection failed for %s", filename)
        if upload is None:
            upload = ImageUpload(filename=filename, batch_tag=batch_tag)
            db.add(upload)
        upload.error = f"detection failed: {exc}"
        db.commit()
        return UploadFileResult(upload_id=upload.id, filename=filename, error=upload.error)

    return _cards_from_detections(
        filename, image_bytes, detections, db,
        verify=settings.verify_identification if verify is None else verify,
        batch_tag=batch_tag, photo_taken_at=photo_taken_at,
        from_grid=grid is not None, upload=upload, progress=progress,
    )


# --- intake: save originals, skip repeats ----------------------------------------


def _duplicates_dir() -> Path:
    path = INBOX_DIR / "duplicates"
    path.mkdir(parents=True, exist_ok=True)
    return path


def purge_staged_duplicates(max_age_days: int = 7) -> int:
    """Delete repeat uploads parked for a forced retry that never came."""
    import time

    folder = INBOX_DIR / "duplicates"
    if not folder.exists():
        return 0
    cutoff = time.time() - max_age_days * 86400
    n = 0
    for path in folder.iterdir():
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            n += 1
    return n


def _describe_cards(cards: list[Card]) -> str:
    parts = [f"#{c.id} {c.player}" if c.player else f"#{c.id}" for c in cards[:6]]
    more = f" and {len(cards) - 6} more" if len(cards) > 6 else ""
    return ", ".join(parts) + more


def find_earlier_upload(db: Session, digest: str) -> tuple[ImageUpload, list[Card]] | None:
    """An earlier upload of the same bytes that still counts: one that did not
    fail and whose cards were not all deleted."""
    earlier = db.scalars(
        select(ImageUpload)
        .where(ImageUpload.sha256 == digest, ImageUpload.error.is_(None))
        .order_by(ImageUpload.id)
    ).all()
    for up in earlier:
        cards = list(db.scalars(select(Card).where(Card.upload_id == up.id).order_by(Card.id)))
        live = [c for c in cards if c.status != STATUS_DELETED]
        if cards and not live:
            continue
        return up, live
    return None


def duplicate_message(found: tuple[ImageUpload, list[Card]]) -> str:
    up, cards = found
    if cards:
        return f"already uploaded (cards {_describe_cards(cards)}); retry with force to add it again"
    if up.raw_vision_json is None:
        return "already uploaded and waiting to be processed; retry with force to add it twice"
    return "already uploaded (no cards were found in it); retry with force to process it again"


def save_original(db: Session, filename: str, data: bytes, batch_tag: str | None,
                  digest: str) -> ImageUpload:
    """Keep the original in data/inbox/processed under a unique, path-safe name
    and record it, so archive and tools/recrop_rotated.py can find it."""
    stored = _safe_inbox_name(filename)
    photo_archive.INBOX_PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    (photo_archive.INBOX_PROCESSED_DIR / stored).write_bytes(data)
    up = ImageUpload(filename=filename, stored_name=stored, sha256=digest, batch_tag=batch_tag)
    db.add(up)
    db.flush()
    return up


def _summary(result: UploadFileResult) -> str:
    n = len(result.cards)
    parts = [f"{n} card{'s' if n != 1 else ''} found" if n else "no cards found"]
    if result.paired_into:
        k = len(result.paired_into)
        parts.append(f"{k} back{'s' if k != 1 else ''} matched to earlier cards")
    if result.backs_waiting:
        k = result.backs_waiting
        whose = "their fronts" if k != 1 else "its front"
        parts.append(f"{k} back{'s' if k != 1 else ''} waiting for {whose}")
    return "; ".join(parts)


def _jobs_upload_handler(db: Session, job: Job, item: JobItem, progress) -> None:
    """Background job: process one saved photo."""
    p = jobs.params(job)
    item_id = item.id
    up = db.get(ImageUpload, item.upload_id) if item.upload_id else None
    if up is None or not up.stored_name:
        raise jobs.ItemFailed("the saved photo record is missing; upload it again")
    path = photo_archive.INBOX_PROCESSED_DIR / up.stored_name
    if not path.exists():
        raise jobs.ItemFailed("the saved photo is no longer on disk; upload it again")
    # A retry may ask for an even grid split of this one photo ("Split as grid").
    item_grid = (p.get("item_grids") or {}).get(str(item.idx))
    grid = tuple(item_grid or p.get("grid") or ()) or None
    result = _process_image(
        up.filename, path.read_bytes(), db, grid=grid, batch_tag=up.batch_tag,
        upload=up, progress=progress, verify=p.get("verify"),
    )
    if result.error:
        raise jobs.ItemFailed(result.error)
    item = db.get(JobItem, item_id)
    jobs.set_card_ids(item, [c.id for c in result.cards])
    item.message = _summary(result)


def _jobs_upload_retry(db: Session, job: Job, item: JobItem, force: bool) -> None:
    """Before a retry: a skipped repeat (forced) gets saved and recorded now; a
    failed photo drops the preview cards its failed attempt left behind."""
    if item.duplicate:
        staged = Path(item.staged_path) if item.staged_path else None
        if staged is None or not staged.exists():
            raise jobs.RetryRefused("the photo is no longer on disk; upload it again with force")
        data = staged.read_bytes()
        p = jobs.params(job)
        up = save_original(db, item.filename, data, p.get("batch_tag"), images.sha256(data))
        staged.unlink(missing_ok=True)
        item.upload_id = up.id
        item.duplicate = False
        item.staged_path = None
        jobs.set_card_ids(item, [])
        return
    if item.upload_id:
        leftovers = db.scalars(
            select(Card).where(Card.upload_id == item.upload_id, Card.status == STATUS_PREVIEW)
        ).all()
        for card in leftovers:
            cropping.delete_crop(card.crop_path)
            db.delete(card)
        up = db.get(ImageUpload, item.upload_id)
        if up is not None:
            up.error = None
    jobs.set_card_ids(item, [])


jobs.register(JOB_UPLOAD, _jobs_upload_handler, _jobs_upload_retry)


def _flag(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


@router.post("/upload")
async def upload(
    files: list[UploadFile] = File(...),
    grid_rows: int = Form(default=0),
    grid_cols: int = Form(default=0),
    batch_tag: str = Form(default=""),
    force: str = Form(default=""),
    db: Session = Depends(get_db),
) -> dict:
    """Save the photos and queue them; returns the job at once.

    Each original is kept in data/inbox/processed (so it archives later) with
    its SHA-256; a photo uploaded before is skipped and reported unless
    `force=true`. HEIC photos are converted to JPEG first. A single background
    worker then processes the photos one by one; poll GET /api/jobs/{job_id}.
    """
    # Optional even-grid split: when both dims are given, slice each photo into
    # rows×cols equal cells instead of auto-detecting boxes. Capped to keep a
    # stray value from producing a runaway number of crops.
    grid: list[int] | None = None
    if grid_rows > 0 and grid_cols > 0:
        grid = [min(grid_rows, 10), min(grid_cols, 10)]
    tag = _clean_tag(batch_tag)
    forced = _flag(force)

    job = jobs.new_job(db, JOB_UPLOAD, {"grid": grid, "batch_tag": tag, "force": forced})
    for f in files:
        data = await f.read()
        name = _safe_source_name(f.filename)
        if not data:
            jobs.add_item(db, job, name, state=ITEM_FAILED, message="empty file")
            continue
        digest = images.sha256(data)
        if images.is_heic(name, data):
            try:
                data = await run_in_threadpool(images.heic_to_jpeg, data)
                name = images.jpeg_name(name)
            except images.UnsupportedImage as exc:
                jobs.add_item(db, job, name, state=ITEM_FAILED, message=str(exc))
                continue
        found = None if forced else find_earlier_upload(db, digest)
        if found is not None:
            staged = _duplicates_dir() / _safe_inbox_name(name)
            staged.write_bytes(data)
            item = jobs.add_item(
                db, job, name, state=ITEM_DONE, duplicate=True,
                staged_path=str(staged), message=duplicate_message(found),
                upload_id=found[0].id,
            )
            jobs.set_card_ids(item, [c.id for c in found[1]])
            continue
        up = save_original(db, name, data, tag, digest)
        jobs.add_item(db, job, name, upload_id=up.id)
    if all(i.state != ITEM_WAITING for i in job.items):
        job.finished_at = jobs.now()
    db.commit()
    job_id = job.id
    jobs.kick()
    db.expire_all()
    return jobs.serialize(db.get(Job, job_id))


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
    verify: str = Form(default=""),
    force: str = Form(default=""),
    db: Session = Depends(get_db),
) -> UploadFileResult:
    """Ingest cards that were identified OUTSIDE the app (e.g. by Claude Code
    reading a folder of photos on a subscription, with no API key here).

    Accepts the original image plus a JSON string of detections — either
    {"cards": [...]} or a bare list — matching the schema in
    app/prompts/card_detection.py. The server still crops and prices each card
    and lands it as a `preview` to review/add, exactly like a photo upload.
    One photo per call, so this one stays synchronous.

    The image is copied into data/inbox/processed under a unique name (so it
    archives later wherever the folder ingest found it). A photo ingested
    before is refused with 409 unless `force=true`.

    `verify` runs the second-pass identity check on each front, like an upload.
    Blank follows VERIFY_IDENTIFICATION (on by default); "false"/"0"/"no"/"off"
    turns it off for this request. The check needs a vision provider in the
    app; without one it is skipped and the skip is recorded on the card.
    """
    try:
        parsed = vision.parse_detection(detections)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"Invalid detections JSON: {exc}")
    if not parsed:
        raise HTTPException(status_code=422, detail="No cards found in detections JSON.")

    image_bytes = await image.read()
    name = _safe_source_name(image.filename)
    digest = images.sha256(image_bytes)
    if images.is_heic(name, image_bytes):
        try:
            image_bytes = await run_in_threadpool(images.heic_to_jpeg, image_bytes)
            name = images.jpeg_name(name)
        except images.UnsupportedImage as exc:
            raise HTTPException(status_code=415, detail=str(exc))
    if not _flag(force):
        found = find_earlier_upload(db, digest)
        if found is not None:
            raise HTTPException(status_code=409, detail=duplicate_message(found))
    photo_taken_at = exif.extract_datetime(image_bytes)
    flag = verify.strip().lower()
    do_verify = (
        get_settings().verify_identification if not flag
        else flag not in ("0", "false", "no", "off")
    )
    tag = _clean_tag(batch_tag)
    up = save_original(db, name, image_bytes, tag, digest)
    db.commit()
    # Cropping, verification and pricing block, so keep them off the event loop.
    return await run_in_threadpool(
        _cards_from_detections,
        name, image_bytes, parsed, db,
        verify=do_verify, batch_tag=tag,
        photo_taken_at=photo_taken_at, upload=up,
    )
