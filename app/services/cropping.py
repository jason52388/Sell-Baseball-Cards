"""Crop a detected card out of the full image using its normalized bbox.

After the bbox crop we optionally REFINE to the card's actual rectangle with
OpenCV (find the card/toploader quad and perspective-warp it straight), which
removes leftover background and deskews tilted cards. If OpenCV isn't available
or no clean card rectangle is found, we fall back to the padded bbox crop.
"""
from __future__ import annotations

import io
import logging
import uuid
from pathlib import Path

from PIL import Image, ImageOps

from app.config import CROPS_DIR, get_settings

logger = logging.getLogger("cropping")

try:  # OpenCV is optional — degrade gracefully to the padded bbox crop.
    import cv2
    import numpy as np
    _CV_OK = True
except Exception:  # noqa: BLE001
    _CV_OK = False


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _order_quad(pts):
    """Order 4 points as top-left, top-right, bottom-right, bottom-left."""
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array(
        [pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]],
        dtype="float32",
    )


def _refine_and_deskew(pil_img: "Image.Image") -> "Image.Image | None":
    """Detect the card's rectangle inside the crop and warp it straight.

    Returns a deskewed PIL image, or None if no confident card-like quad is
    found (caller then keeps the padded bbox crop). Conservative on purpose: it
    only fires when a 4-corner shape dominates the crop and has a card-like
    aspect ratio, so it never makes a good crop worse."""
    if not _CV_OK:
        return None
    try:
        bgr = cv2.cvtColor(np.array(pil_img.convert("RGB")), cv2.COLOR_RGB2BGR)
        h, w = bgr.shape[:2]
        area_img = float(h * w)
        gray = cv2.GaussianBlur(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), (5, 5), 0)
        edges = cv2.dilate(cv2.Canny(gray, 40, 140), np.ones((5, 5), np.uint8), iterations=2)
        cnts, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in sorted(cnts, key=cv2.contourArea, reverse=True)[:5]:
            area = cv2.contourArea(c)
            if area < 0.45 * area_img or area > 0.99 * area_img:
                continue  # too small (artifact) or basically the whole frame
            approx = cv2.approxPolyDP(c, 0.02 * cv2.arcLength(c, True), True)
            if len(approx) != 4 or not cv2.isContourConvex(approx):
                continue
            quad = _order_quad(approx.reshape(4, 2).astype("float32"))
            tl, tr, br, bl = quad
            out_w = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
            out_h = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
            if out_w < 40 or out_h < 40:
                continue
            ar = out_w / out_h
            if not (0.55 <= ar <= 0.80 or 1.25 <= ar <= 1.82):  # portrait or landscape card
                continue
            dst = np.array(
                [[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]],
                dtype="float32",
            )
            warped = cv2.warpPerspective(bgr, cv2.getPerspectiveTransform(quad, dst), (out_w, out_h))
            return Image.fromarray(cv2.cvtColor(warped, cv2.COLOR_BGR2RGB))
    except Exception:  # noqa: BLE001
        logger.debug("card refine/deskew failed; using padded crop", exc_info=True)
    return None


def assess_quality(image: "str | Path | Image.Image") -> str:
    """Cheap photo-quality read of a crop: flags 'glare' (blown-out highlights)
    and 'blurry' (low edge detail). Returns 'good' or a comma-joined label."""
    if not _CV_OK:
        return "good"
    try:
        img = Image.open(image) if isinstance(image, (str, Path)) else image
        gray = cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2GRAY)
        # Normalize size so the blur threshold is resolution-independent.
        scale = 600.0 / max(gray.shape)
        if scale < 1:
            gray = cv2.resize(gray, (0, 0), fx=scale, fy=scale)
        blown = float((gray >= 250).mean())          # fraction of pure-white pixels
        sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        flags = []
        if blown > 0.03:
            flags.append("glare")
        if sharp < 60:
            flags.append("blurry")
        return ", ".join(flags) if flags else "good"
    except Exception:  # noqa: BLE001
        return "good"


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
    # Refine to the card's actual rectangle (deskew + tighten) when possible.
    if get_settings().crop_autostraighten:
        refined = _refine_and_deskew(crop)
        if refined is not None:
            crop = refined
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
