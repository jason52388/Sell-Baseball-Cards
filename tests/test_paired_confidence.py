"""Confidence is recomputed from the combined front + back identity.

Fronts rarely print the year or number, so an honest front-only read lands
around 0.55-0.68, under the 0.7 pricing gate, even when the back read both
clearly. Pairing must lift it when the two sides agree, never lower it, and
unmatching must put the front's own confidence back.
"""
import json

import pytest

from app.models import Card, ImageUpload
from app.services import pairing


def _audit(conf, **reads):
    return json.dumps({
        "confidence": conf,
        "field_reads": {k: {"value": v[0], "confidence": v[1]} for k, v in reads.items()},
    })


def _front(**kw):
    defaults = dict(
        side="front", player="Pete Rose", set_brand="Topps", confidence=0.6,
        identification_json=_audit(0.6, player=("Pete Rose", 0.95), set_brand=("Topps", 0.8)),
    )
    defaults.update(kw)
    return Card(**defaults)


def _back(**kw):
    defaults = dict(
        side="back", player="Pete Rose", year="1989", card_number="505",
        set_brand="Topps", confidence=0.85,
        identification_json=_audit(
            0.85, player=("Pete Rose", 0.9), year=("1989", 0.9),
            card_number=("505", 0.9), set_brand=("Topps", 0.7),
        ),
    )
    defaults.update(kw)
    return Card(**defaults)


def test_agreeing_back_lifts_confidence_capped_by_the_stronger_side():
    front, back = _front(), _back()
    pairing.remember_pre_pair_identity(front)
    pairing.enrich_front_from_back(front, back)
    # weighted best-per-field = 0.8975, capped at max(0.6, 0.85)
    assert front.confidence == 0.85
    assert front.year == "1989" and front.card_number == "505"


def test_weighted_score_used_when_below_the_cap():
    front = _front()
    back = _back(identification_json=_audit(
        0.95, player=("Pete Rose", 0.9), year=("1989", 0.7), card_number=("505", 0.6),
    ), confidence=0.95, set_brand=None)
    pairing.enrich_front_from_back(front, back)
    # 0.35*.95 + 0.2*.7 + 0.2*.8 + 0.25*.6 = 0.7825
    assert front.confidence == pytest.approx(0.7825)


def test_different_players_do_not_raise():
    front, back = _front(), _back(player="Mark McGwire", identification_json=_audit(
        0.9, player=("Mark McGwire", 0.9), year=("1989", 0.9), card_number=("505", 0.9)))
    pairing.enrich_front_from_back(front, back)
    assert front.confidence == 0.6


def test_conflicting_year_does_not_raise():
    front = _front(year="1990", identification_json=_audit(
        0.6, player=("Pete Rose", 0.95), year=("1990", 0.5)))
    pairing.enrich_front_from_back(front, _back())
    assert front.confidence == 0.6


def test_conflicting_number_does_not_raise():
    front = _front(card_number="12", identification_json=_audit(
        0.6, player=("Pete Rose", 0.95), card_number=("12", 0.4)))
    pairing.enrich_front_from_back(front, _back())
    assert front.confidence == 0.6


def test_pairing_never_lowers_confidence():
    front = _front(confidence=0.92)
    back = _back(confidence=0.4, identification_json=_audit(0.4, year=("1989", 0.3)))
    pairing.enrich_front_from_back(front, back)
    assert front.confidence == 0.92


def test_verifier_disagreement_is_not_overridden_by_pairing():
    audit = json.loads(_front().identification_json)
    audit["verification"] = {"agree": False, "notes": "name differs"}
    front = _front(confidence=0.4, identification_json=json.dumps(audit))
    pairing.enrich_front_from_back(front, _back())
    assert front.confidence == 0.4


def test_back_confidence_falls_back_to_its_field_reads():
    """Backs stored before the audit carried an overall confidence."""
    back = _back(confidence=None, identification_json=json.dumps({"field_reads": {
        "player": {"value": "Pete Rose", "confidence": 0.9},
        "year": {"value": "1989", "confidence": 0.9},
        "card_number": {"value": "505", "confidence": 0.9},
    }}))
    front = _front()
    pairing.enrich_front_from_back(front, back)
    # back overall = mean of its core reads (0.9), which caps the 0.9175 score
    assert front.confidence == pytest.approx(0.9)


def test_unmatch_restores_pre_pair_confidence():
    front, back = _front(), _back()
    pairing.remember_pre_pair_identity(front)
    pairing.enrich_front_from_back(front, back)
    assert front.confidence == 0.85
    pairing.restore_pre_pair_identity(front, back.identification_json)
    assert front.confidence == 0.6
    assert front.year is None and front.card_number is None


def test_snapshot_with_only_confidence_restores_fields_from_own_reading():
    """A front paired before snapshots existed gets a confidence-only snapshot
    from the recompute tool; unmatch must still revert the lent fields."""
    front = _front(year="1989", card_number="505", confidence=0.85,
                   pre_pair_identity_json=json.dumps({"confidence": 0.6}))
    pairing.restore_pre_pair_identity(front, _back().identification_json)
    assert front.confidence == 0.6
    assert front.year is None and front.card_number is None


def test_try_pair_lifts_confidence(db_session):
    up = ImageUpload(filename="f.jpg")
    db_session.add(up)
    db_session.flush()
    front = _front(upload_id=up.id, year="1989")
    front.identification_json = _audit(0.6, player=("Pete Rose", 0.95),
                                       year=("1989", 0.6), set_brand=("Topps", 0.8))
    db_session.add(front)
    db_session.flush()
    back = _back(upload_id=up.id)
    db_session.add(back)
    db_session.flush()
    assert pairing.try_pair(back, db_session) is front
    assert front.confidence == 0.85


def test_manual_attach_lifts_confidence(db_session, monkeypatch):
    from app.routers import cards

    monkeypatch.setattr(cards, "reprice_after_pairing", lambda card, db: card)
    up = ImageUpload(filename="f.jpg")
    db_session.add(up)
    db_session.flush()
    front, back = _front(upload_id=up.id), _back(upload_id=up.id)
    db_session.add_all([front, back])
    db_session.flush()
    cards._attach_back(front, back, db_session)
    assert front.confidence == 0.85
    assert json.loads(front.pre_pair_identity_json)["confidence"] == 0.6


def test_detection_audit_records_overall_confidence(db_session, monkeypatch):
    """New cards keep their own overall confidence in the audit, so a later
    pairing can tell the side's read apart from a raised value."""
    from app.routers import upload
    from app.schemas import DetectedCard

    monkeypatch.setattr(upload.cropping, "crop_card", lambda *a, **k: None)
    monkeypatch.setattr(upload, "preview_card", lambda card, db: None)
    upload._cards_from_detections(
        "p.jpg", b"", [DetectedCard(player="A", confidence=0.64, bbox=[0, 0, 0.5, 0.5])],
        db_session, verify=False,
    )
    card = db_session.query(Card).one()
    assert json.loads(card.identification_json)["confidence"] == 0.64
