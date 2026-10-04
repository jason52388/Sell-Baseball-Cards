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

A different player on each side is NOT a mismatch by itself: league-leader and
combo cards show one player on the front and another on the back. Two signals
do rule a pairing out (see _contradicts and _claimed_elsewhere): different
players whose years are more than one apart, and, for the timestamp fallback, a
candidate whose own identity already matches some other card.
"""
from __future__ import annotations

import json
import logging
import re

from sqlalchemy.orm import Session

from app.models import STATUS_DELETED, Card, ImageUpload

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


_NAME_NOISE = {"jr", "sr", "ii", "iii", "iv", "the", "and"}


def _name_tokens(player: str | None) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (player or "").lower())
    return {w for w in words if len(w) >= 3 and w not in _NAME_NOISE}


def players_differ(a: Card, b: Card) -> bool:
    """Do both sides name a player, and share no name between them?

    Multi-player cards list names in different orders, or only some of them, on
    each side, so any shared name counts as agreement. An unread player on
    either side is not a difference."""
    ta, tb = _name_tokens(a.player), _name_tokens(b.player)
    return bool(ta and tb and not (ta & tb))


def _year(card: Card) -> int | None:
    m = re.search(r"\d{4}", card.year or "")
    return int(m.group()) if m else None


# Backs often print the prior year's copyright, so the two sides of one card
# routinely read a year apart.
_SAME_CARD_YEAR_SLACK = 1


def _contradicts(a: Card, b: Card) -> bool:
    """Can a and b NOT be two sides of one card?

    A different player alone is allowed (league-leader and combo cards put a
    second player on the back, printed in the same year). A different player
    whose year is also more than a year off is a different card: the 1989 Pete
    Rose back is not the back of the 1997 Halladay prospects front."""
    if not players_differ(a, b):
        return False
    ya, yb = _year(a), _year(b)
    return ya is not None and yb is not None and abs(ya - yb) > _SAME_CARD_YEAR_SLACK


def _claimed_elsewhere(candidate: Card, rivals: list[Card]) -> bool:
    """Does the candidate's own identity match one of the rival cards?

    A back that reads "1989 Topps #505 Pete Rose" belongs to a Pete Rose front
    even when two copies of that front made the identity match ambiguous; it
    must not fall to whichever front happened to be photographed next."""
    return any(
        _shares_key(candidate, r, "yn") or _shares_key(candidate, r, "yp")
        for r in rivals
    )


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


def _unique_match(
    card: Card, candidates: list[Card], rivals: list[Card] | None = None
) -> Card | None:
    """The single candidate that matches `card`, or None if zero/ambiguous.

    Prefer the STRONG (year + card number) key — backs almost always print the
    number, so this is reliable. Only fall back to the WEAK (year + player) key
    when the strong key finds nothing. If MORE THAN ONE candidate matches a key
    (e.g. a box full of the same player/year), we refuse to guess and return
    None so the user pairs it manually — better no back than the wrong back.

    Final fallback: EXIF timestamp proximity — photos taken within a few seconds
    are likely the same physical card flipped over.

    `rivals` are the other cards on this card's side that could still take a
    candidate. A candidate that contradicts this card (see _contradicts) is
    dropped before any key is tried, and the timestamp fallback skips any
    candidate whose identity already matches a rival. A wrong back is worse
    than none: it also overwrites the front's number and price.
    """
    candidates = [c for c in candidates if not _contradicts(card, c)]
    strong = [c for c in candidates if _shares_key(card, c, "yn")]
    if len(strong) == 1:
        return strong[0]
    if len(strong) > 1:
        return None  # ambiguous on the strong key -> don't guess
    weak = [c for c in candidates if _shares_key(card, c, "yp")]
    if len(weak) == 1:
        return weak[0]
    # Timestamp fallback: photos taken seconds apart are likely the same card
    unclaimed = [c for c in candidates if not _claimed_elsewhere(c, rivals or [])]
    ts_match = _closest_by_timestamp(card, unclaimed)
    if ts_match is not None:
        logger.info("timestamp-paired cards (%.0fs apart)",
                    abs((card.photo_taken_at - ts_match.photo_taken_at).total_seconds()))
    return ts_match


def remember_back_source(front: Card, back: Card, db: Session) -> None:
    """Record where the back came from onto the front (inside its
    back-identification audit), since the back row itself is deleted:

    - `_source_filename` / `_stored_name` / `_upload_id`: the back's own photo,
      so it archives alongside the front's and a detach gives the back its own
      upload again (not the front's);
    - `_photo_taken_at` / `_batch_tag`: restored on detach so the back can still
      re-pair by timestamp and keeps its batch.
    """
    try:
        audit = json.loads(front.back_identification_json or "{}")
    except Exception:  # noqa: BLE001
        audit = {}
    if not isinstance(audit, dict):
        audit = {}
    up = db.get(ImageUpload, back.upload_id) if back.upload_id else None
    if up and up.filename:
        audit["_source_filename"] = up.filename
    if up and up.stored_name:
        audit["_stored_name"] = up.stored_name
    if back.upload_id:
        audit["_upload_id"] = back.upload_id
    if back.photo_taken_at:
        audit["_photo_taken_at"] = back.photo_taken_at.isoformat()
    if back.batch_tag:
        audit["_batch_tag"] = back.batch_tag
    front.back_identification_json = json.dumps(audit)


# Identity fields a pairing may overwrite or backfill on the front.
_PAIRED_IDENTITY_FIELDS = (
    "year", "card_number", "set_brand", "parallel", "sport", "team", "subset",
)


def remember_pre_pair_identity(front: Card) -> None:
    """Snapshot the front's own identity before a back overwrites it.

    Only the first snapshot is kept: re-pairing a front that already carries a
    borrowed identity must still be able to get back to the card's own reading.
    """
    if front.pre_pair_identity_json:
        return
    snapshot = {f: getattr(front, f, None) for f in _PAIRED_IDENTITY_FIELDS}
    # Pairing may raise the confidence (see recompute_paired_confidence), so the
    # front's own confidence is part of what unmatching must restore.
    snapshot["confidence"] = front.confidence
    front.pre_pair_identity_json = json.dumps(snapshot)


def _field_reads(audit_json: str | None) -> dict:
    try:
        audit = json.loads(audit_json or "{}")
    except Exception:  # noqa: BLE001
        return {}
    reads = audit.get("field_reads") if isinstance(audit, dict) else None
    return reads if isinstance(reads, dict) else {}


def _read_value(reads: dict, field: str) -> str | None:
    v = reads.get(field)
    return (v.get("value") if isinstance(v, dict) else None) or None


def _restore_from_own_reading(front: Card, back_audit_json: str | None) -> bool:
    """Fallback for a front paired before snapshots existed: any field that
    still equals what the back read, and differs from what the front's own photo
    read, was lent by the back, so it goes back to the front's own reading. A
    value the user has since typed no longer equals the back's, so it stays."""
    own = _field_reads(front.identification_json)
    lent = _field_reads(back_audit_json)
    changed = False
    for field in _PAIRED_IDENTITY_FIELDS:
        current, from_back = getattr(front, field, None), _read_value(lent, field)
        if current and current == from_back and _read_value(own, field) != current:
            setattr(front, field, _read_value(own, field))
            changed = True
    return changed


def restore_pre_pair_identity(front: Card, back_audit_json: str | None = None) -> bool:
    """Put back the identity the front had before it was paired.

    Uses the snapshot taken at pairing time. A front paired before snapshots
    existed falls back to its own photo's reading, given the detached back's
    identification audit; without that it is left as is (no data loss)."""
    if not front.pre_pair_identity_json:
        return _restore_from_own_reading(front, back_audit_json)
    try:
        saved = json.loads(front.pre_pair_identity_json)
    except Exception:  # noqa: BLE001
        logger.warning("unreadable pre-pair identity on card %s", front.id)
        front.pre_pair_identity_json = None
        return _restore_from_own_reading(front, back_audit_json)
    if isinstance(saved, dict):
        if any(field in saved for field in _PAIRED_IDENTITY_FIELDS):
            for field in _PAIRED_IDENTITY_FIELDS:
                if field in saved:
                    setattr(front, field, saved.get(field))
        else:
            # A confidence-only snapshot (written by the recompute tool for a
            # front paired before snapshots existed): fields come back from the
            # front's own reading.
            _restore_from_own_reading(front, back_audit_json)
        if saved.get("confidence") is not None:
            front.confidence = saved["confidence"]
    front.pre_pair_identity_json = None
    return True


# --- Confidence of the combined identity --------------------------------------
#
# The confidence stored at detection is the FRONT's read alone. Fronts rarely
# print the year or card number, so an honest front read sits around 0.55-0.68,
# below the pricing gate, even when the back read both clearly. Once a back is
# paired the card's identity is the combination of both sides, so its confidence
# is recomputed from that combination:
#
#   1. Per core field, take the best confidence among the sides whose reading
#      equals the value the card now carries.
#   2. Combine them as a weighted mean (_CORE_WEIGHTS; player and number weigh
#      most because they decide the price match).
#   3. Cap the result at the stronger side's own overall confidence, so pairing
#      can never claim more certainty than either read had on its own.
#   4. Only ever raise: if the result is not above the current confidence,
#      nothing changes.
#
# Nothing is raised when the sides contradict each other (different players,
# years or numbers both read) or when the verifier already disagreed with the
# front. The front's pre-pair confidence is in its pre-pair snapshot, so
# unmatching restores it.

_CORE_WEIGHTS = {"player": 0.35, "year": 0.20, "set_brand": 0.20, "card_number": 0.25}


def _norm_field(field: str, value) -> str:
    s = str(value or "").strip().lower()
    if field == "card_number":
        return s.lstrip("#").strip()
    if field == "year":
        m = re.search(r"\d{4}", s)
        return m.group() if m else s
    return _norm(s)


def _audit(audit_json: str | None) -> dict:
    try:
        audit = json.loads(audit_json or "{}")
    except Exception:  # noqa: BLE001
        return {}
    return audit if isinstance(audit, dict) else {}


def _side_values(
    audit: dict, fallback_values: dict, fallback_conf: float
) -> dict[str, tuple[str, float]]:
    """{field: (value, confidence)} for the core fields one side read.

    Per-field reads win; a value known only from the card row (no per-field
    read) counts at the side's overall confidence."""
    reads = audit.get("field_reads") if isinstance(audit.get("field_reads"), dict) else {}
    out: dict[str, tuple[str, float]] = {}
    for field in _CORE_WEIGHTS:
        r = reads.get(field)
        value = r.get("value") if isinstance(r, dict) else None
        if value:
            try:
                conf = float(r.get("confidence") if r.get("confidence") is not None
                             else fallback_conf)
            except (TypeError, ValueError):
                conf = fallback_conf
            out[field] = (str(value), conf)
        elif fallback_values.get(field):
            out[field] = (str(fallback_values[field]), fallback_conf)
    return out


def _overall(audit: dict, explicit: float | None) -> float:
    """A side's overall confidence: the explicit value, else the one stored in
    its audit, else the mean of its core per-field reads (older audits)."""
    for v in (explicit, audit.get("confidence")):
        if isinstance(v, (int, float)):
            return float(v)
    reads = audit.get("field_reads") if isinstance(audit.get("field_reads"), dict) else {}
    confs = [
        float(r["confidence"]) for f, r in reads.items()
        if f in _CORE_WEIGHTS and isinstance(r, dict) and r.get("value")
        and isinstance(r.get("confidence"), (int, float))
    ]
    return sum(confs) / len(confs) if confs else 0.0


def _sides_contradict(front: dict, back: dict) -> bool:
    fp, bp = front.get("player"), back.get("player")
    if fp and bp:
        ta, tb = _name_tokens(fp[0]), _name_tokens(bp[0])
        if ta and tb and not (ta & tb):
            return True
    for field in ("year", "card_number"):
        fv, bv = front.get(field), back.get(field)
        if fv and bv and _norm_field(field, fv[0]) != _norm_field(field, bv[0]):
            return True
    return False


def combined_confidence(
    front: Card, back_audit_json: str | None,
    back_values: dict | None = None, back_conf: float | None = None,
) -> float | None:
    """The combined front + back confidence under the rule above, or None when
    the sides contradict (or the verifier disagreed) and nothing may be raised.
    Does not modify the card."""
    front_audit = _audit(front.identification_json)
    verification = front_audit.get("verification")
    if isinstance(verification, dict) and verification.get("agree") is False:
        return None
    snapshot = _audit(front.pre_pair_identity_json)
    # The front's own (pre-pair) confidence: snapshot, else detection audit,
    # else the stored value (an unpaired front has not been raised yet).
    own_conf = next(
        (v for v in (snapshot.get("confidence"), front_audit.get("confidence"),
                     front.confidence) if isinstance(v, (int, float))),
        None,
    )
    front_conf = _overall(front_audit, own_conf)
    # The front's own values: its pre-pair snapshot if present, else only the
    # player (never lent by a back). Card attrs may already carry back values.
    own = {"player": front.player}
    if any(f in snapshot for f in _PAIRED_IDENTITY_FIELDS):
        own.update({f: snapshot.get(f) for f in _CORE_WEIGHTS if f != "player"})
    front_vals = _side_values(front_audit, own, front_conf)

    back_audit = _audit(back_audit_json)
    b_conf = _overall(back_audit, back_conf)
    back_vals = _side_values(back_audit, back_values or {}, b_conf)
    if _sides_contradict(front_vals, back_vals):
        return None

    score = 0.0
    for field, weight in _CORE_WEIGHTS.items():
        current = _norm_field(field, getattr(front, field, None))
        if not current:
            continue
        best = max(
            (conf for vals in (front_vals, back_vals)
             for f, (value, conf) in vals.items()
             if f == field and _norm_field(field, value) == current),
            default=0.0,
        )
        score += weight * max(0.0, min(1.0, best))
    return round(min(score, max(front_conf, b_conf)), 4)


def back_supplied_fields(front: Card) -> dict[str, float]:
    """{field: confidence} for identity fields the front carries because its
    paired back supplied them, with the back's confidence in each value.

    Uses the pre-pair snapshot (a field that differs from the front's own
    pre-pair value came from the back); without a snapshot, a field equal to
    the back's reading and different from the front's own reading. Re-analysis
    uses this to keep a back-supplied value unless a new read is more sure."""
    if not front.back_identification_json:
        return {}
    back_audit = _audit(front.back_identification_json)
    back_reads = _field_reads(front.back_identification_json)
    back_conf = _overall(back_audit, None)
    snapshot = _audit(front.pre_pair_identity_json)
    has_fields = any(f in snapshot for f in _PAIRED_IDENTITY_FIELDS)
    own_reads = _field_reads(front.identification_json)
    out: dict[str, float] = {}
    for field in _PAIRED_IDENTITY_FIELDS:
        current = getattr(front, field, None)
        if not current:
            continue
        if has_fields:
            if _norm_field(field, snapshot.get(field)) == _norm_field(field, current):
                continue
        else:
            if _read_value(back_reads, field) != current or _read_value(own_reads, field) == current:
                continue
        read = back_reads.get(field)
        conf = back_conf
        if isinstance(read, dict) and isinstance(read.get("confidence"), (int, float)) \
                and _norm_field(field, read.get("value")) == _norm_field(field, current):
            conf = float(read["confidence"])
        out[field] = conf
    return out


def recompute_paired_confidence(
    front: Card, back_audit_json: str | None,
    back_values: dict | None = None, back_conf: float | None = None,
) -> bool:
    """Raise the front's confidence to the combined identity's, if higher.
    Returns True if it changed. Caller re-prices."""
    new = combined_confidence(front, back_audit_json, back_values, back_conf)
    if new is None or new <= (front.confidence or 0.0):
        return False
    front.confidence = new
    return True


def enrich_front_from_back(front: Card, back: Card) -> bool:
    """Backfill the front's MISSING identity fields from its back (the back often
    prints the year/number the front omits), then recompute the confidence of
    the combined identity (recompute_paired_confidence). Returns True if
    anything changed — the caller should then re-price, since a newly-known
    number sharpens the market match. Never overwrites a value the front
    already has."""
    changed = False
    for attr in _PAIRED_IDENTITY_FIELDS:
        if not getattr(front, attr, None) and getattr(back, attr, None):
            setattr(front, attr, getattr(back, attr))
            changed = True
    back_values = {f: getattr(back, f, None) for f in _CORE_WEIGHTS}
    if recompute_paired_confidence(
        front, back.identification_json, back_values, back.confidence
    ):
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
            .filter(Card.side == "front", Card.back_crop_path.is_(None), Card.id != card.id,
                    Card.status != STATUS_DELETED)
            .all()
        )
        rivals = (
            db.query(Card)
            .filter(Card.side == "back", Card.id != card.id, Card.status != STATUS_DELETED)
            .all()
        )
        front = _unique_match(card, fronts, rivals)
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

    # card is a front: pull in the single matching orphan back. A front that
    # already carries a back keeps it (absorbing a second would overwrite it).
    if card.back_crop_path:
        return None
    backs = (
        db.query(Card)
        .filter(Card.side == "back", Card.id != card.id, Card.status != STATUS_DELETED)
        .all()
    )
    rivals = (
        db.query(Card)
        .filter(Card.side == "front", Card.back_crop_path.is_(None), Card.id != card.id,
                Card.status != STATUS_DELETED)
        .all()
    )
    back = _unique_match(card, backs, rivals)
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
