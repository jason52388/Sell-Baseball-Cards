"""Archive card photos into a user-configured collection folder (e.g. iCloud
Drive) when a card is added to the repository.

Two kinds of file are archived:

* **Source photos** — the original camera images are MOVED out of the working
  inbox so they don't pile up.
* **Crop images** — the individual front/back card crops are COPIED into the
  collection folder so the user has nicely named reference images (the app
  still needs the originals in data/crops).

Photos are renamed to match the card description (e.g.
``Mike Trout, Topps Chrome, 2023, Refractor (front).jpg``).

Best-effort: any failure is logged and skipped — archiving a photo must never
block adding a card.
"""
from __future__ import annotations

import logging
import re
import shutil
import sqlite3
from pathlib import Path

from app.config import DATA_DIR, INBOX_PROCESSED_DIR, get_settings

logger = logging.getLogger("photo_archive")


def _dest_dir() -> Path | None:
    raw = (get_settings().collection_photos_dir or "").strip()
    return Path(raw).expanduser() if raw else None


_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _descriptive_name(description: str, side: str, ext: str) -> str:
    """Build a filesystem-safe filename from a card description and side label."""
    slug = _UNSAFE_CHARS.sub("", description).strip()
    slug = re.sub(r"\s+", " ", slug)
    if not slug:
        slug = "card"
    return f"{slug} ({side}){ext}"


def _unique_target(dest: Path, name: str) -> Path:
    """Return ``dest/name``, appending a numeric suffix if it already exists."""
    target = dest / name
    if not target.exists():
        return target
    stem = target.stem
    ext = target.suffix
    n = 2
    while True:
        candidate = dest / f"{stem} ({n}){ext}"
        if not candidate.exists():
            return candidate
        n += 1


def archive_source_files(
    entries: list[tuple[str | None, str, str]],
) -> int:
    """Move each named source photo from data/inbox/processed into the configured
    collection folder, renaming it to match the card description.

    *entries* is a list of ``(original_filename, description, side)`` tuples where
    *description* is the human-readable card identity (e.g.
    ``"Mike Trout, Topps Chrome, 2023, Refractor"``) and *side* is ``"front"``
    or ``"back"``.

    Returns how many files were moved. No-op if the collection dir is unconfigured.
    """
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
    for name, description, side in entries:
        if not name or name == "manual entry" or name in seen:
            continue
        seen.add(name)
        src = INBOX_PROCESSED_DIR / name
        if not src.exists():
            continue
        new_name = _descriptive_name(description, side, src.suffix)
        target = _unique_target(dest, new_name)
        try:
            shutil.move(str(src), str(target))
            moved += 1
        except Exception:  # noqa: BLE001
            logger.exception("failed to archive %s -> %s", src, target)
    return moved


def archive_crop_files(
    entries: list[tuple[str | None, str, str]],
) -> int:
    """Copy front/back crop images into the collection folder with descriptive names.

    *entries* is a list of ``(crop_path, description, side)`` tuples.  Crops are
    COPIED (not moved) because the app still references the originals in
    data/crops for display and re-analysis.

    Returns how many files were copied.
    """
    dest = _dest_dir()
    if dest is None:
        return 0
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        logger.exception("could not create collection photos dir %s", dest)
        return 0

    copied = 0
    for path_str, description, side in entries:
        if not path_str:
            continue
        src = Path(path_str)
        if not src.exists():
            continue
        new_name = _descriptive_name(description, side, src.suffix)
        target = _unique_target(dest, new_name)
        try:
            shutil.copy2(str(src), str(target))
            copied += 1
        except Exception:  # noqa: BLE001
            logger.exception("failed to copy crop %s -> %s", src, target)
    return copied


def backup_database() -> bool:
    """Copy cards.db into the collection folder using SQLite's online backup API.

    This produces a consistent snapshot even if the database is in WAL mode or
    being read concurrently.  The backup overwrites any previous copy so the
    collection folder always has the latest version.

    Returns True on success, False (with a log message) on failure or if the
    collection dir is unconfigured.
    """
    dest = _dest_dir()
    if dest is None:
        return False
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        logger.exception("could not create collection photos dir %s", dest)
        return False

    src_path = DATA_DIR / "cards.db"
    if not src_path.exists():
        return False
    target = dest / "cards-backup.db"
    try:
        src_conn = sqlite3.connect(str(src_path))
        dst_conn = sqlite3.connect(str(target))
        with dst_conn:
            src_conn.backup(dst_conn)
        dst_conn.close()
        src_conn.close()
        return True
    except Exception:  # noqa: BLE001
        logger.exception("failed to backup database to %s", target)
        return False
