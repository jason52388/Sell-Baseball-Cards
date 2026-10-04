"""Recompute the confidence of cards already paired with their back.

Until pairing recomputed confidence, a paired card kept its front-only score.
Fronts rarely print the year or number, so honest scores sat at 0.55-0.68,
under the 0.7 pricing gate, even when the back read both clearly. This tool
applies the pairing rule (`pairing.combined_confidence`, documented there and
in the ingest skill) to every paired card. It only ever raises a confidence,
and never when the two sides contradict each other.

The old confidence is saved in the card's pre-pair snapshot, so unmatching the
back later still restores it.

Dry run by default (prints old -> new). `--apply` writes. `--reprice` (with
`--apply`) also re-prices the raised cards: previews stay previews, library
cards are re-priced and re-routed, cards with a live listing are not touched.
Re-pricing calls the price sources, so it needs network.

Usage:
  python -m tools.recompute_paired_confidence --data-dir data
  python -m tools.recompute_paired_confidence --data-dir data --apply --reprice
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import STATUS_LIST_FAILED, STATUS_LISTED, STATUS_PREVIEW, Card
from app.services.pairing import combined_confidence
from app.services.pricing import preview_card, price_card

logger = logging.getLogger("recompute_paired_confidence")


def _snapshot_confidence(card: Card) -> None:
    """Record the card's current confidence in its pre-pair snapshot (creating a
    confidence-only snapshot if the card was paired before snapshots existed)."""
    try:
        snap = json.loads(card.pre_pair_identity_json or "{}")
    except Exception:  # noqa: BLE001
        snap = {}
    if not isinstance(snap, dict):
        snap = {}
    if snap.get("confidence") is None:
        snap["confidence"] = card.confidence
    card.pre_pair_identity_json = json.dumps(snap)


def _reprice(card: Card, db: Session) -> None:
    if card.status in (STATUS_LISTED, STATUS_LIST_FAILED) or card.is_listed:
        return
    for comp in list(card.comps):
        db.delete(comp)
    db.flush()
    card.review_reason = None  # the old "low identification confidence" note
    if card.status == STATUS_PREVIEW:
        preview_card(card, db)
    else:
        price_card(card, db)


def run(db: Session, apply: bool = False, reprice: bool = False) -> list[dict]:
    """Plan (and with apply=True, write) the new confidence for paired cards.
    Returns one dict per card that would change: card_id, player, old, new,
    status."""
    plan: list[dict] = []
    cards = (
        db.query(Card)
        .filter(Card.side == "front", Card.back_crop_path.isnot(None))
        .order_by(Card.id)
        .all()
    )
    for card in cards:
        new = combined_confidence(card, card.back_identification_json)
        old = card.confidence or 0.0
        if new is None or new <= old:
            continue
        plan.append({
            "card_id": card.id, "player": card.player, "old": card.confidence,
            "new": new, "status": card.status,
        })
        if not apply:
            continue
        _snapshot_confidence(card)
        card.confidence = new
        if reprice:
            try:
                _reprice(card, db)
            except Exception:  # noqa: BLE001
                logger.exception("re-price failed for card %s", card.id)
    if apply:
        db.commit()
    return plan


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", type=Path, default=Path("data"))
    ap.add_argument("--apply", action="store_true", help="write the new confidences")
    ap.add_argument("--reprice", action="store_true", help="with --apply, re-price raised cards")
    args = ap.parse_args()

    from tools._db import open_session

    db = open_session(args.data_dir)
    plan = run(db, apply=args.apply, reprice=args.reprice and args.apply)
    for p in plan:
        print(f"card {p['card_id']:>5}  {p['player'] or '?':<28} {p['status']:<14} "
              f"confidence {p['old']} -> {p['new']}")
    verb = "updated" if args.apply else "would update (dry run; add --apply)"
    print(f"{len(plan)} paired card(s) {verb}")


if __name__ == "__main__":
    main()
