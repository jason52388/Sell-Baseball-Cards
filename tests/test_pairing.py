"""Front/back pairing accuracy: unique-match only, cross-side enrichment, and
the phantom-detection filter that keeps junk crops out."""
from app.models import Card
from app.routers.upload import _is_phantom_detection
from app.schemas import DetectedCard
from app.services.pairing import (
    _unique_match,
    enrich_front_from_back,
)


def _front(**kw):
    return Card(side="front", **kw)


def _back(**kw):
    return Card(side="back", **kw)


# --- unique-match: never guess when ambiguous ---

def test_pairs_unique_back_on_strong_key():
    back = _back(year="2001", card_number="189", player="Kerry Wood")
    fronts = [
        _front(year="2001", card_number="189", player="Kerry Wood"),
        _front(year="2001", card_number="42", player="Barry Bonds"),
    ]
    assert _unique_match(back, fronts) is fronts[0]


def test_refuses_ambiguous_weak_key():
    # Two 2001 Kerry Wood fronts, neither with a number -> ambiguous -> no pair.
    back = _back(year="2001", player="Kerry Wood")
    fronts = [
        _front(year="2001", player="Kerry Wood"),
        _front(year="2001", player="Kerry Wood"),
    ]
    assert _unique_match(back, fronts) is None


def test_weak_key_used_only_when_unique():
    back = _back(year="2001", player="Kerry Wood")
    fronts = [
        _front(year="2001", player="Kerry Wood"),
        _front(year="2001", player="Barry Bonds"),
    ]
    assert _unique_match(back, fronts) is fronts[0]


def test_no_match_returns_none():
    back = _back(year="2001", player="Sammy Sosa")
    fronts = [_front(year="1999", player="Barry Bonds")]
    assert _unique_match(back, fronts) is None


# --- a back whose printed player contradicts the front never pairs ---

def _at(seconds):
    from datetime import datetime, timedelta
    return datetime(2026, 8, 23, 21, 7, 30) + timedelta(seconds=seconds)


def test_timestamp_fallback_refuses_a_different_player():
    # The Pete Rose back was shot seconds before the Halladay prospects front:
    # close in time, but the two sides name different players.
    front = _front(player="Roy Halladay / Matt Clement / Brian Fuentes", year="1997",
                   photo_taken_at=_at(13))
    backs = [_back(player="Pete Rose", year="1989", card_number="505",
                   photo_taken_at=_at(5))]
    assert _unique_match(front, backs) is None


def test_strong_key_refuses_a_different_player():
    # Same year and number, different set: a coincidence, not the same card.
    back = _back(year="2000", card_number="462", player="Mark McGwire")
    fronts = [_front(year="2000", card_number="462", player="Ken Griffey Jr.")]
    assert _unique_match(back, fronts) is None


def test_timestamp_fallback_still_pairs_same_player():
    front = _front(player="Johnny Damon", photo_taken_at=_at(0))
    backs = [_back(player="Johnny Damon", year="2001", card_number="59",
                   photo_taken_at=_at(4))]
    assert _unique_match(front, backs) is backs[0]


def test_multi_player_card_pairs_with_back_naming_one_of_them():
    # Fronts and backs list multi-player cards in different orders or partially.
    front = _front(player="Sammy Sosa, Troy Glaus", photo_taken_at=_at(0))
    backs = [_back(player="Sammy Sosa", year="2001", photo_taken_at=_at(3))]
    assert _unique_match(front, backs) is backs[0]


def test_unread_player_on_one_side_is_not_a_contradiction():
    front = _front(player="Michael Jordan", photo_taken_at=_at(0))
    backs = [_back(player=None, year="1998", card_number="175", photo_taken_at=_at(3))]
    assert _unique_match(front, backs) is backs[0]


# --- cross-side enrichment: backfill the front's missing fields from the back ---

def test_enrich_fills_missing_number_and_year():
    front = _front(player="Kerry Wood")  # front omitted the number/year
    back = _back(player="Kerry Wood", year="1997", card_number="189", set_brand="Upper Deck")
    changed = enrich_front_from_back(front, back)
    assert changed is True
    assert front.year == "1997"
    assert front.card_number == "189"
    assert front.set_brand == "Upper Deck"


def test_enrich_never_overwrites_existing():
    front = _front(player="Kerry Wood", year="2001", card_number="42")
    back = _back(player="Kerry Wood", year="1997", card_number="189")
    enrich_front_from_back(front, back)
    assert front.year == "2001" and front.card_number == "42"  # untouched


# --- phantom-detection filter: keep junk crops out ---

def test_phantom_tiny_bbox_rejected():
    det = DetectedCard(player="x", confidence=0.6, bbox=[0.0, 0.57, 0.07, 0.13])  # ~0.9% area
    assert _is_phantom_detection(det) is True


def test_phantom_low_conf_no_identity_rejected():
    det = DetectedCard(confidence=0.1, bbox=[0.1, 0.1, 0.5, 0.5])
    assert _is_phantom_detection(det) is True


def test_real_card_kept():
    det = DetectedCard(player="Kerry Wood", confidence=0.6, bbox=[0.07, 0.02, 0.86, 0.92])
    assert _is_phantom_detection(det) is False


# --- unmatching a pair made before pre-pair snapshots existed ---

def _audit(**reads):
    import json
    return json.dumps({"field_reads": {k: {"value": v, "confidence": 0.9}
                                       for k, v in reads.items()}})


def test_restore_without_snapshot_drops_only_what_the_back_lent():
    from app.services.pairing import restore_pre_pair_identity
    # Halladay front read no number; the Pete Rose back lent it #505. The year
    # was the front's own reading and differs from the back's, so it stays.
    front = _front(player="Roy Halladay", year="1997", card_number="505",
                   set_brand="Topps",
                   identification_json=_audit(year="1997", card_number=None,
                                              set_brand="Topps"))
    back_audit = _audit(player="Pete Rose", year="1989", card_number="505",
                        set_brand="Topps")
    assert restore_pre_pair_identity(front, back_audit) is True
    assert front.card_number is None
    assert front.year == "1997"
    assert front.set_brand == "Topps"


def test_restore_without_snapshot_keeps_a_hand_edit():
    from app.services.pairing import restore_pre_pair_identity
    # The user typed #12 after pairing: it no longer equals the back's value,
    # so unmatching must not touch it.
    front = _front(card_number="12", identification_json=_audit(card_number=None))
    back_audit = _audit(card_number="505")
    restore_pre_pair_identity(front, back_audit)
    assert front.card_number == "12"
