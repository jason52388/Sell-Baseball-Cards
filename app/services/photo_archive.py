"""Archive a card's ORIGINAL source photo(s) out of the working inbox and into a
user-configured collection folder (e.g. iCloud Drive) once the card is added to
the repository.

Best-effort: any failure is logged and skipped — archiving a photo must never
block adding a card. Crops are independent copies in data/crops, so moving the
source photo does not affect re-cropping/re-analysis or other cards.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from app.config import INBOX_PROCESSED_DIR, get_settings

logger = logging.getLogger("photo_archive")


def _dest_dir() -> Path | None:
    raw = (get_settings().collection_photos_dir or "").strip()
    return Path(raw).expanduser() if raw else None


def archive_source_files(filenames: list[str | None]) -> int:
    """Move each named source photo from data/inbox/processed into the configured
    collection folder. Returns how many were moved. No-op if unconfigured."""
    dest = _dest_dir()
    if dest is None:
        return 0
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        logger.exception("could not create collection photos dir %s", dest)
        return 0

    moved = 0
    seen: set[str] = set()
    for name in filenames:
        if not name or name == "manual entry" or name in seen:
            continue
        seen.add(name)
        src = INBOX_PROCESSED_DIR / name
        if not src.exists():
            continue  # already archived, or never had a source file
        target = dest / name
        if target.exists():
            # Already archived under this name — drop the working copy.
            try:
                src.unlink()
            except Exception:  # noqa: BLE001
                logger.exception("could not remove already-archived %s", src)
            continue
        try:
            shutil.move(str(src), str(target))
            moved += 1
        except Exception:  # noqa: BLE001
            logger.exception("failed to archive %s -> %s", src, target)
    return moved
