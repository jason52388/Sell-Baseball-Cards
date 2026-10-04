"""The second-pass verifier: unknown is not disagreement, confident evidenced
corrections are applied, failures are visible, and the back is shown."""
import io
import json

import pytest
from PIL import Image

from app.models import Card
from app.routers import upload
from app.schemas import DetectedCard, VerificationResult


def _jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), (200, 120, 60)).save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture
def run(db_session, monkeypatch):
    monkeypatch.setattr(upload, "preview_card", lambda card, db: card)
    monkeypatch.setattr(upload, "reprice_after_pairing", lambda card, db: card)

    def _run(det, result=None, exc=None, seen=None):
        def fake_verify(crop, proposed, back_bytes=None):
            if seen is not None:
                seen.append((proposed, back_bytes))
            if exc:
                raise exc
            return result

        monkeypatch.setattr(upload.vision, "verify_card", fake_verify)
        upload._cards_from_detections("p.jpg", _jpeg(), [det], db_session, verify=True)
        return db_session.query(Card).filter(Card.side == "front").order_by(Card.id.desc()).first()

    return _run


def _det(**kw):
    base = dict(player="Pete Rose", year="1989", set_brand="Topps", card_number="505",
                confidence=0.9, bbox=[0, 0, 0.5, 0.5])
    base.update(kw)
    return DetectedCard(**base)


def _verification(card):
    return json.loads(card.identification_json)["verification"]


def test_unknown_does_not_lower_confidence(run):
    card = run(_det(), VerificationResult(agree=None, unverifiable=["year"]))
    assert card.confidence == 0.9


def test_disagreement_without_correction_caps_confidence(run):
    card = run(_det(), VerificationResult(agree=False, notes="name differs"))
    assert card.confidence == 0.4
    assert _verification(card)["agree"] is False


def test_confident_evidenced_correction_is_applied(run):
    card = run(_det(), VerificationResult(agree=False, corrections={
        "year": {"value": "1990", "confidence": 0.95, "reason": "copyright reads 1990"}}))
    assert card.year == "1990"
    v = _verification(card)
    assert v["applied"] == {"year": {"from": "1989", "to": "1990"}}
    assert card.confidence == 0.9  # every disagreement was resolved with evidence


def test_correction_without_reason_is_only_flagged(run):
    card = run(_det(), VerificationResult(agree=False, corrections={"year": "1990"}))
    assert card.year == "1989"
    assert _verification(card)["flagged"] == ["year"]
    assert card.confidence == 0.4


def test_low_confidence_correction_is_only_flagged(run):
    card = run(_det(), VerificationResult(agree=False, corrections={
        "card_number": {"value": "50", "confidence": 0.5, "reason": "maybe 50"}}))
    assert card.card_number == "505"
    assert card.confidence == 0.4


def test_failure_is_recorded_in_the_audit(run):
    card = run(_det(), exc=RuntimeError("claude CLI failed: timeout"))
    assert "timeout" in _verification(card)["error"]
    assert card.confidence == 0.9


def test_verifier_sees_the_back_when_one_is_already_paired(run, db_session):
    seen = []
    # Back photographed first, waiting for its front.
    run(_det(side="back", player="Pete Rose", confidence=0.85))
    run(_det(confidence=0.6, card_number=None), VerificationResult(agree=True), seen=seen)
    proposed, back = seen[-1]
    assert back is not None
    assert proposed.card_number == "505"  # verifies the combined identity
