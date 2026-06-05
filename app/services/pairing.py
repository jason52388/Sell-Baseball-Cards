"""Match front and back card scans by identity.

Cards are uploaded as separate front/back photos in any order. Each detected
card is classified front/back (see the detection prompt). This module attaches a
back image to its matching front so the collection shows one card per physical
card, with both sides on the detail page.

Matching is by identity, tolerant of which fields each side prints:
  - strong key: (year, card_number)   — backs almost always print both
  - weak key:   (year, normalized player)
Two cards pair if they share ANY key. Set/brand spelling is ignored because it
often differs between the front and back of the same card.
"""
from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session

from app.models import Card

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


def _matches(a: Card, b: Card) -> bool:
    ka, kb = pair_keys(a), pair_keys(b)
    return bool(ka and kb and (ka & kb))


def try_pair(card: Card, db: Session) -> Card | None:
    """Attach this card to its other side if a match exists.

    - If `card` is a BACK: find a front lacking a back, move this back's image
      onto it, delete this back row, and return the front.
    - If `card` is a FRONT: absorb a matching un-matched back, delete the back
      row, and return the back.
    Returns None if no match (the card stays as-is). Caller commits.
    """
    if not pair_keys(card):
        return None

    if card.side == "back":
        fronts = (
            db.query(Card)
            .filter(Card.side == "front", Card.back_crop_path.is_(None), Card.id != card.id)
            .all()
        )
        for front in fronts:
            if _matches(card, front):
                front.back_crop_path = card.crop_path
                front.back_identification_json = card.identification_json
                db.delete(card)  # crop file is kept; front.back_crop_path points to it
                logger.info("paired back card -> front %s", front.id)
                return front
        return None

    # card is a front: pull in any orphan back
    backs = db.query(Card).filter(Card.side == "back", Card.id != card.id).all()
    for back in backs:
        if _matches(card, back):
            card.back_crop_path = back.crop_path
            card.back_identification_json = back.identification_json
            db.delete(back)
            logger.info("front %s absorbed orphan back %s", card.id, back.id)
            return back
    return None
