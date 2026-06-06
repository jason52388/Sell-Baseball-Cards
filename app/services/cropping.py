"""Crop a detected card out of the full image using its normalized bbox."""
from __future__ import annotations

import io
import uuid
from pathlib import Path

from PIL import Image, ImageOps

from app.config import CROPS_DIR, get_settings


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def crop_card(
    image_bytes: bytes, bbox: list[float], card_id: int, *, pad: float | None = None
) -> str | None:
    """Crop [x, y, w, h] (normalized 0..1) from image_bytes, save as JPEG.

    A safety margin (`pad`, a fraction of the box's own size, default from
    settings.crop_padding_pct) is added on every side before cropping, because
    the vision model's boxes often sit slightly INSIDE the card and shave off an
    edge. Better to include a sliver of background than cut the card off. The
    margin is clamped to the image bounds. Returns the saved path, or None.
    """
    if not bbox or len(bbox) != 4:
        return None
    if pad is None:
        pad = get_settings().crop_padding_pct
    img = Image.open(io.BytesIO(image_bytes))
    # Honor the photo's EXIF orientation (phone cameras store rotation in EXIF
    # rather than rotating pixels). This keeps the crop right-side-up AND aligns
    # our pixel grid with the upright image the vision model scored the bbox on.
    img = ImageOps.exif_transpose(img).convert("RGB")
    w, h = img.size
    px = abs(bbox[2]) * pad  # margin scaled to the box's own width/height
    py = abs(bbox[3]) * pad
    x0 = _clamp(bbox[0] - px) * w
    y0 = _clamp(bbox[1] - py) * h
    x1 = _clamp(bbox[0] + bbox[2] + px) * w
    y1 = _clamp(bbox[1] + bbox[3] + py) * h
    left, right = sorted((int(x0), int(x1)))
    top, bottom = sorted((int(y0), int(y1)))
    if right - left < 2 or bottom - top < 2:
        return None
    crop = img.crop((left, top, right, bottom))
    # Unique filename per crop. SQLite reuses primary-key ids after a row is
    # deleted (e.g. a back card removed during pairing), so naming crops by
    # card_id alone let a reused id OVERWRITE a file another card still pointed
    # at — scrambling backs. A uuid suffix makes every crop file permanent and
    # collision-proof. The card_id prefix keeps it human-readable.
    out_path: Path = CROPS_DIR / f"{card_id}-{uuid.uuid4().hex[:8]}.jpg"
    crop.save(out_path, format="JPEG", quality=90)
    return str(out_path)


def grid_cells(
    image_bytes: bytes, rows: int, cols: int
) -> list[tuple[list[float], bytes]]:
    """Slice an image into rows×cols equal cells, row-major (left→right, top→bottom).

    This is the deterministic alternative to letting the vision model guess each
    card's bounding box: when cards are laid out in a neat grid, an even split is
    far more reliable than AI-detected boxes that drift or overlap.

    Returns, for each cell, its normalized [x, y, w, h] bbox on the full image
    (so the saved crop matches what gets identified) and the cell's JPEG bytes.
    """
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB")
    w, h = img.size
    cells: list[tuple[list[float], bytes]] = []
    for r in range(rows):
        for c in range(cols):
            left, right = round(c * w / cols), round((c + 1) * w / cols)
            top, bottom = round(r * h / rows), round((r + 1) * h / rows)
            cell = img.crop((left, top, right, bottom))
            buf = io.BytesIO()
            cell.save(buf, format="JPEG", quality=90)
            bbox = [c / cols, r / rows, 1 / cols, 1 / rows]
            cells.append((bbox, buf.getvalue()))
    return cells


def read_crop_bytes(path: str) -> bytes:
    return Path(path).read_bytes()


def delete_crop(path: str | None) -> None:
    """Remove a card's crop file from disk if present. Best-effort; never raises."""
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass
