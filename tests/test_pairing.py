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
