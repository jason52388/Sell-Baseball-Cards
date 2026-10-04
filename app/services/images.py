"""Uploaded image helpers: HEIC conversion and content hashing.

iPhones save photos as HEIC. Pillow cannot read HEIC on its own, so an
uploaded HEIC is converted to JPEG before anything else touches it: with
pillow-heif when it is installed, else with macOS's built-in `sips`. With
neither, the photo fails with a clear message instead of a vague detection
error.
"""
from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import tempfile
from pathlib import Path

HEIC_UNSUPPORTED = "HEIC not supported here, export the photo as JPG and upload that"

# ISO-BMFF brands used by HEIC/HEIF files (bytes 4..12 are "ftyp" + brand).
_HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}


class UnsupportedImage(Exception):
    """The image is in a format this machine cannot convert."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_heic(filename: str | None, data: bytes) -> bool:
    ext = Path(filename or "").suffix.lower()
    if ext in (".heic", ".heif"):
        return True
    return len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in _HEIC_BRANDS


def _with_pillow_heif(data: bytes) -> bytes | None:
    try:
        import pillow_heif  # type: ignore[import-not-found]
    except ImportError:
        return None
    from PIL import Image

    pillow_heif.register_heif_opener()
    img = Image.open(io.BytesIO(data))
    exif = img.info.get("exif")
    out = io.BytesIO()
    kwargs = {"exif": exif} if exif else {}
    img.convert("RGB").save(out, format="JPEG", quality=95, **kwargs)
    return out.getvalue()


def _with_sips(data: bytes) -> bytes | None:
    sips = shutil.which("sips")
    if not sips:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "in.heic", Path(tmp) / "out.jpg"
        src.write_bytes(data)
        proc = subprocess.run(
            [sips, "-s", "format", "jpeg", str(src), "--out", str(dst)],
            capture_output=True, timeout=120,
        )
        if proc.returncode != 0 or not dst.exists():
            return None
        return dst.read_bytes()


def heic_to_jpeg(data: bytes) -> bytes:
    """Convert HEIC bytes to JPEG (EXIF kept). Raises UnsupportedImage."""
    for convert in (_with_pillow_heif, _with_sips):
        try:
            out = convert(data)
        except Exception:  # noqa: BLE001 - try the next converter
            out = None
        if out:
            return out
    raise UnsupportedImage(HEIC_UNSUPPORTED)


def jpeg_name(filename: str) -> str:
    stem = Path(filename).stem or "photo"
    return f"{stem}.jpg"
