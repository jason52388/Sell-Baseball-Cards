"""Match front and back card scans by identity or photo timestamp.

Cards are uploaded as separate front/back photos in any order. Each detected
card is classified front/back (see the detection prompt). This module attaches a
back image to its matching front so the collection shows one card per physical
card, with both sides on the detail page.

Matching is by identity, tolerant of which fields each side prints:
  - strong key: (year, card_number)   — backs almost always print both
  - weak key:   (year, normalized player)
Two cards pair if they share ANY key. Set/brand spelling is ignored because it
often differs between the front and back of the same card.

Fallback: if identity keys don't match, photos taken within a few seconds of
each other (EXIF DateTimeOriginal) are likely front/back of the same card.
"""
from __future__ import annotations

import json
import logging
import re

from sqlalchemy.orm import Session

from app.models import Card, ImageUpload

logger = logging.getLogger("pairing")


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def pair_keys(card: Card) -> set[tuple]:
    """Identity keys this card can be matched on (empty if too little info)."""
    keys: set[tuple] = set()
    year = (card.year or "").strip()
    number = (card.card_number or "").lstrip("#").strip().lower()
    player = _norm(card.player)
    if year and number:
        keys.add(("yn", year, number))
    if year and player:
        keys.add(("yp", year, player))
    return keys


def _shares_key(a: Card, b: Card, prefix: str) -> bool:
    """Do a and b share a pairing key of the given kind ('yn' or 'yp')?"""
    ka = {k for k in pair_keys(a) if k[0] == prefix}
    kb = {k for k in pair_keys(b) if k[0] == prefix}
    return bool(ka and (ka & kb))


_TIMESTAMP_PAIR_SECONDS = 10


def _closest_by_timestamp(card: Card, candidates: list[Card]) -> Card | None:
    """The single candidate whose photo was taken within a few seconds of `card`.

    Returns None if the card has no timestamp, no candidates have timestamps,
    or more than one candidate is within the window (ambiguous).
    """
    if not card.photo_taken_at:
        return None
    within = []
    for c in candidates:
        if not c.photo_taken_at:
            continue
        delta = abs((card.photo_taken_at - c.photo_taken_at).total_seconds())
        if delta <= _TIMESTAMP_PAIR_SECONDS:
            within.append((delta, c))
    if len(within) == 1:
        return within[0][1]
    if len(within) > 1:
        within.sort(key=lambda t: t[0])
        # Only auto-pair if the closest is clearly nearer than the runner-up
        if within[0][0] < within[1][0] - 2:
            return within[0][1]
    return None


def _unique_match(card: Card, candidates: list[Card]) -> Card | None:
    """The single candidate that matches `card`, or None if zero/ambiguous.

    Prefer the STRONG (year + card number) key — backs almost always print the
    number, so this is reliable. Only fall back to the WEAK (year + player) key
    when the strong key finds nothing. If MORE THAN ONE candidate matches a key
    (e.g. a box full of the same player/year), we refuse to guess and return
    None so the user pairs it manually — better no back than the wrong back.

    Final fallback: EXIF timestamp proximity — photos taken within a few seconds
    are likely the same physical card flipped over.
    """
    strong = [c for c in candidates if _shares_key(card, c, "yn")]
    if len(strong) == 1:
        return strong[0]
    if len(strong) > 1:
        return None  # ambiguous on the strong key -> don't guess
    weak = [c for c in candidates if _shares_key(card, c, "yp")]
    if len(weak) == 1:
        return weak[0]
    # Timestamp fallback: photos taken seconds apart are likely the same card
    ts_match = _closest_by_timestamp(card, candidates)
    if ts_match is not None:
        logger.info("timestamp-paired cards (%.0fs apart)",
                    abs((card.photo_taken_at - ts_match.photo_taken_at).total_seconds()))
    return ts_match


def remember_back_source(front: Card, back: Card, db: Session) -> None:
    """Record the back's ORIGINAL source-photo filename onto the front (inside its
    back-identification audit) so that, when the card is later added to the
    collection, the back's source photo can be archived alongside the front's."""
    up = db.get(ImageUpload, back.upload_id) if back.upload_id else None
    if not up or not up.filename:
        return
    try:
        audit = json.loads(front.back_identification_json or "{}")
    except Exception:  # noqa: BLE001
        audit = {}
    if not isinstance(audit, dict):
        audit = {}
    audit["_source_filename"] = up.filename
    front.back_identification_json = json.dumps(audit)


# Identity fields a pairing may overwrite or backfill on the front.
_PAIRED_IDENTITY_FIELDS = ("year", "card_number", "set_brand", "parallel", "sport")


def remember_pre_pair_identity(front: Card) -> None:
    """Snapshot the front's own identity before a back overwrites it.

    Only the first snapshot is kept: re-pairing a front that already carries a
    borrowed identity must still be able to get back to the card's own reading.
    """
    if front.pre_pair_identity_json:
        return
    front.pre_pair_identity_json = json.dumps(
        {f: getattr(front, f, None) for f in _PAIRED_IDENTITY_FIELDS}
    )


def restore_pre_pair_identity(front: Card) -> bool:
    """Put back the identity the front had before it was paired. No-op (and no
    data loss) for a front paired before snapshots existed."""
    if not front.pre_pair_identity_json:
        return False
    try:
        saved = json.loads(front.pre_pair_identity_json)
    except Exception:  # noqa: BLE001
        logger.warning("unreadable pre-pair identity on card %s", front.id)
        front.pre_pair_identity_json = None
        return False
    if isinstance(saved, dict):
        for field in _PAIRED_IDENTITY_FIELDS:
            setattr(front, field, saved.get(field))
    front.pre_pair_identity_json = None
    return True


def enrich_front_from_back(front: Card, back: Card) -> bool:
    """Backfill the front's MISSING identity fields from its back (the back often
    prints the year/number the front omits). Returns True if anything changed —
    the caller should then re-price, since a newly-known number sharpens the
    market match. Never overwrites a value the front already has."""
    changed = False
    for attr in ("year", "card_number", "set_brand", "parallel", "sport"):
        if not getattr(front, attr, None) and getattr(back, attr, None):
            setattr(front, attr, getattr(back, attr))
            changed = True
    return changed


def try_pair(card: Card, db: Session) -> Card | None:
    """Attach this card to its other side if exactly one match exists.

    - If `card` is a BACK: find the single matching front lacking a back, move
      this back's image onto it, enrich+return the front, delete this back row.
    - If `card` is a FRONT: absorb the single matching un-matched back and
      return it (the front, `card`, is enriched in place).
    Returns None if no/ambiguous match. The returned front may have enriched
    identity (see enrich_front_from_back) — the caller should re-price it.
    Caller commits.
    """
    if not pair_keys(card):
        return None

    if card.side == "back":
        fronts = (
            db.query(Card)
            .filter(Card.side == "front", Card.back_crop_path.is_(None), Card.id != card.id)
            .all()
        )
        front = _unique_match(card, fronts)
        if front is None:
            return None
        front.back_crop_path = card.crop_path
        front.back_identification_json = card.identification_json
        remember_pre_pair_identity(front)
        enrich_front_from_back(front, card)
        remember_back_source(front, card, db)
        db.delete(card)  # crop file is kept; front.back_crop_path points to it
        logger.info("paired back card -> front %s", front.id)
        return front

    # card is a front: pull in the single matching orphan back
    backs = db.query(Card).filter(Card.side == "back", Card.id != card.id).all()
    back = _unique_match(card, backs)
    if back is None:
        return None
    card.back_crop_path = back.crop_path
    card.back_identification_json = back.identification_json
    remember_pre_pair_identity(card)
    enrich_front_from_back(card, back)
    remember_back_source(card, back, db)
    db.delete(back)
    logger.info("front %s absorbed orphan back %s", card.id, back.id)
    return back
