"""Re-cut card crops that were sliced by the sideways-photo bug.

Before the fix in `vision._upright`, phone photos (pixels stored sideways plus
an EXIF rotation flag) went to the vision model raw, so every bounding box was
read off the sideways pixels while the cropper cut from the upright photo. The
crops landed on the wrong part of the photo and cut cards off.

This tool rotates each stored box onto the upright photo
(`cropping.bbox_from_raw_pixels`) and re-cuts the front and back crops from the
original photos with today's padding rules. No model calls are made.

Covers cards in preview and in the library whose source photo is still in
`data/inbox/processed/`. Cards with a published eBay listing are skipped (their
listing already carries its photos). Old crop files are left on disk and an
undo log of every path change is written next to the database.

Safe to re-run: a crop listed as `new` in any earlier undo log is skipped, so
nothing is rotated twice. `--apply` also stores the upright box in `bbox_json`.

Usage:
  python -m tools.recrop_rotated --data-dir /path/to/data --preview out.jpg
  python -m tools.recrop_rotated --data-dir /path/to/data --apply
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

from app.config import get_settings
from app.routers.upload import _is_phantom_detection
from app.schemas import DetectedCard
from app.services import cropping


def _orientation(path: Path) -> int:
    with Image.open(path) as img:
        return img.getexif().get(0x0112, 1)


def _real_detections(raw_json: str | None) -> list[DetectedCard]:
    dets = [DetectedCard.model_validate(d) for d in json.loads(raw_json or "[]")]
    return [d for d in dets if not _is_phantom_detection(d)]


def _already_redone(data_dir: Path) -> set[str]:
    done: set[str] = set()
    for log in data_dir.glob("recrop-undo-*.json"):
        done.update(e["new"] for e in json.loads(log.read_text()))
    return done


def _plan(db: sqlite3.Connection, processed: Path, done: set[str]) -> list[dict]:
    """Every crop to re-cut: card id, which side, source photo, upright bbox, pad."""
    settings = get_settings()
    uploads = {
        r[0]: (r[1], r[2])
        for r in db.execute("select id, filename, raw_vision_json from image_uploads")
    }
    latest_by_name: dict[str, int] = {}
    for uid, (name, _) in sorted(uploads.items()):
        latest_by_name[name] = uid
    listed = {r[0] for r in db.execute("select card_id from listings where status='published'")}

    def job(card_id, field, old, upload_id, bbox, want_side):
        name, raw = uploads.get(upload_id, (None, None))
        src = processed / name if name else None
        if not src or not src.exists():
            return None
        orient = _orientation(src)
        if orient == 1:
            return None  # upright photo: the box was always right
        real = _real_detections(raw)
        if bbox is None:  # a paired back: find its box in the back photo's detections
            sided = [d for d in real if d.side == want_side] or real
            if len(sided) != 1 or not sided[0].bbox:
                return None
            bbox = sided[0].bbox
        pad = settings.single_card_pad_pct if len(real) == 1 else None
        return {
            "card_id": card_id, "field": field, "old": old, "src": str(src),
            "orientation": orient, "bbox": cropping.bbox_from_raw_pixels(bbox, orient),
            "pad": pad, "name_id": int(Path(old).name.split("-")[0]),
        }

    jobs = []
    rows = db.execute(
        "select id, upload_id, bbox_json, crop_path, back_crop_path, back_identification_json "
        "from cards where crop_path is not null"
    ).fetchall()
    for cid, upload_id, bbox_json, crop, back_crop, back_audit in rows:
        if cid in listed:
            continue
        if crop in done:
            bbox_json = None  # front already re-cut
        if back_crop in done:
            back_crop = None
        if bbox_json:
            j = job(cid, "crop_path", crop, upload_id, json.loads(bbox_json), None)
            if j:
                jobs.append(j)
        if back_crop:
            src_name = json.loads(back_audit or "{}").get("_source_filename")
            back_upload = latest_by_name.get(src_name)
            if back_upload:
                j = job(cid, "back_crop_path", back_crop, back_upload, None, "back")
                if j:
                    jobs.append(j)
    return jobs


def _cut(job: dict, out_dir: Path) -> str | None:
    cropping.CROPS_DIR = out_dir
    data = Path(job["src"]).read_bytes()
    return cropping.crop_card(data, job["bbox"], job["name_id"], pad=job["pad"])


def _sheet(pairs: list[tuple[str, str, str]], out: Path) -> None:
    """Old crop next to new crop, one row per job, for a visual check."""
    h = 300
    rows = []
    for label, old, new in pairs:
        ims = []
        for p in (old, new):
            im = ImageOps.exif_transpose(Image.open(p)).convert("RGB")
            ims.append(im.resize((max(1, int(im.width * h / im.height)), h)))
        row = Image.new("RGB", (ims[0].width + ims[1].width + 30, h), "white")
        row.paste(ims[0], (0, 0))
        row.paste(ims[1], (ims[0].width + 30, 0))
        rows.append(row)
    width = max(r.width for r in rows)
    sheet = Image.new("RGB", (width, (h + 12) * len(rows)), (90, 90, 90))
    for i, r in enumerate(rows):
        sheet.paste(r, (0, i * (h + 12)))
    sheet.save(out, quality=80)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", type=Path, required=True)
    ap.add_argument("--preview", type=Path, help="write an old-vs-new contact sheet here")
    ap.add_argument("--apply", action="store_true", help="save new crops and update cards")
    args = ap.parse_args()
    args.data_dir = args.data_dir.resolve()  # cards store absolute crop paths

    db = sqlite3.connect(args.data_dir / "cards.db")
    jobs = _plan(db, args.data_dir / "inbox" / "processed", _already_redone(args.data_dir))
    print(f"{len(jobs)} crops to re-cut")

    if not args.apply:
        out_dir = Path(tempfile.mkdtemp(prefix="recrop-"))
        pairs = []
        for j in jobs:
            new = _cut(j, out_dir)
            if new:
                pairs.append((f"{j['card_id']} {j['field']}", j["old"], new))
        if args.preview and pairs:
            _sheet(pairs, args.preview)
            print(f"preview: {args.preview}")
        return

    crops_dir = args.data_dir / "crops"
    undo = []
    for j in jobs:
        new = _cut(j, crops_dir)
        if not new:
            continue
        db.execute(f"update cards set {j['field']} = ? where id = ?", (new, j["card_id"]))
        entry = {"card_id": j["card_id"], "field": j["field"], "old": j["old"], "new": new}
        if j["field"] == "crop_path":
            old_bbox = db.execute("select bbox_json from cards where id = ?", (j["card_id"],))
            entry["old_bbox"] = json.loads(old_bbox.fetchone()[0])
            entry["new_bbox"] = j["bbox"]
            db.execute(
                "update cards set bbox_json = ? where id = ?",
                (json.dumps(j["bbox"]), j["card_id"]),
            )
        undo.append(entry)
    db.commit()
    log = args.data_dir / f"recrop-undo-{datetime.now():%Y%m%d-%H%M%S}.json"
    log.write_text(json.dumps(undo, indent=2))
    print(f"updated {len(undo)} crops; undo log: {log}")


if __name__ == "__main__":
    main()
