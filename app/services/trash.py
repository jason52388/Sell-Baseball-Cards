"""Soft delete: a deleted card is hidden, restorable for a week, then purged.

Deleting a card used to remove its row, comps, listing records and crop files
at once, with no way back. Now a delete only marks the card `deleted` (keeping
the status it had, for restore) and stamps `deleted_at`. Every list, count,
pairing search and review query skips deleted cards. On startup,
`purge_expired` removes cards deleted more than TRASH_RETENTION_DAYS ago, with
their crop files.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import STATUS_DELETED, STATUS_PREVIEW, TRASH_RETENTION_DAYS, Card
from app.services import cropping

logger = logging.getLogger("trash")


class RestoreExpired(Exception):
    """The card was deleted too long ago (or is already purged)."""


def _now() -> datetime:
    # SQLite hands DateTime back naive, so store and compare naive UTC.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def restore_until(card: Card) -> datetime | None:
    if card.deleted_at is None:
        return None
    return _naive(card.deleted_at) + timedelta(days=TRASH_RETENTION_DAYS)


def soft_delete(card: Card) -> None:
    """Mark the card deleted. Caller commits. Crop files stay for a restore."""
    if card.status == STATUS_DELETED:
        return
    card.status_before_delete = card.status
    card.status = STATUS_DELETED
    card.deleted_at = _now()


def restore(card: Card) -> None:
    """Undo a soft delete within the retention window. Caller commits."""
    if card.status != STATUS_DELETED:
        return
    until = restore_until(card)
    if until is not None and _now() > until:
        raise RestoreExpired(
            f"Deleted more than {TRASH_RETENTION_DAYS} days ago; it can no longer be restored."
        )
    card.status = card.status_before_delete or STATUS_PREVIEW
    card.status_before_delete = None
    card.deleted_at = None


def purge_expired(db: Session) -> int:
    """Permanently remove cards deleted more than TRASH_RETENTION_DAYS ago,
    with their comps, listing records and crop files. Returns how many."""
    cutoff = _now() - timedelta(days=TRASH_RETENTION_DAYS)
    rows = list(db.scalars(
        select(Card).where(Card.status == STATUS_DELETED, Card.deleted_at < cutoff)
    ))
    paths: list[str] = []
    for card in rows:
        paths += [p for p in (card.crop_path, card.back_crop_path) if p]
        db.delete(card)
    db.commit()
    for path in paths:
        cropping.delete_crop(path)
    if rows:
        logger.info("purged %d card(s) deleted over %d days ago", len(rows), TRASH_RETENTION_DAYS)
    return len(rows)
