"""Write an upright copy of a photo for the folder ingest to hand to Claude.

Phone photos store their pixels sideways plus an EXIF flag saying how to turn
them. The cropper applies that flag, so the boxes Claude returns must be read
off the upright photo too, whether or not Claude's image reader honors the flag.
`tools/ingest_folder.sh` shows Claude this copy and still uploads the original,
which keeps the EXIF timestamp that front/back pairing relies on.

Usage:
  python -m tools.upright_copy PHOTO WORK_DIR   # prints the copy's path
"""
from __future__ import annotations

import sys
from pathlib import Path

from app.services.vision import _upright


def write_upright_copy(src: Path, work_dir: Path) -> Path:
    data = src.read_bytes()
    upright = _upright(data)
    # _upright re-encodes as JPEG only when it had to turn the photo.
    suffix = src.suffix.lower() if upright is data else ".jpg"
    work_dir.mkdir(parents=True, exist_ok=True)
    out = work_dir / f"{src.stem}{suffix}"
    out.write_bytes(upright)
    return out


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    print(write_upright_copy(Path(sys.argv[1]), Path(sys.argv[2])))
