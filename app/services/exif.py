"""Extract EXIF DateTimeOriginal from JPEG bytes without external dependencies."""
from __future__ import annotations

import re
import struct
from datetime import datetime


def extract_datetime(image_bytes: bytes) -> datetime | None:
    """Return the EXIF DateTimeOriginal (or DateTime) from JPEG bytes, or None."""
    # Find the Exif APP1 marker (0xFFE1)
    idx = image_bytes.find(b"\xff\xe1")
    if idx < 0:
        return None
    # APP1 segment: 2-byte length, then "Exif\x00\x00", then TIFF header
    if idx + 10 > len(image_bytes):
        return None
    # Just regex for a YYYY:MM:DD HH:MM:SS pattern in the first 64KB of EXIF
    chunk = image_bytes[idx : idx + 65536]
    m = re.search(rb"(20[012]\d:[01]\d:[0-3]\d [0-2]\d:[0-5]\d:[0-5]\d)", chunk)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1).decode(), "%Y:%m:%d %H:%M:%S")
    except (ValueError, UnicodeDecodeError):
        return None
