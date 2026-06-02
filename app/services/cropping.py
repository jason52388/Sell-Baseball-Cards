"""Crop a detected card out of the full image using its normalized bbox."""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from app.config import CROPS_DIR


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def crop_card(image_bytes: bytes, bbox: list[float], card_id: int) -> str | None:
    """Crop [x, y, w, h] (normalized 0..1) from image_bytes, save as JPEG.

    Returns the saved path as a string, or None if the bbox is unusable.
    """
    if not bbox or len(bbox) != 4:
        return None
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = img.size
    x0 = _clamp(bbox[0]) * w
    y0 = _clamp(bbox[1]) * h
    x1 = _clamp(bbox[0] + bbox[2]) * w
    y1 = _clamp(bbox[1] + bbox[3]) * h
    left, right = sorted((int(x0), int(x1)))
    top, bottom = sorted((int(y0), int(y1)))
    if right - left < 2 or bottom - top < 2:
        return None
    crop = img.crop((left, top, right, bottom))
    out_path: Path = CROPS_DIR / f"{card_id}.jpg"
    crop.save(out_path, format="JPEG", quality=90)
    return str(out_path)


def read_crop_bytes(path: str) -> bytes:
    return Path(path).read_bytes()
