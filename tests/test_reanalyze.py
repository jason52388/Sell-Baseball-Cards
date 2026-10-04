"""Re-analysis: sees both sides, keeps what the back supplied unless the new
read is surer, works on library cards, refuses listed ones, picks the central
card in a crop."""
import json

import pytest

from app.db import get_db
from app.main import app
from app.models import Card, Listing
from app.schemas import DetectedCard, FieldRead
from app.services import vision
from tests.test_api import _ingest_one, client  # noqa: F401  (fixture)


def _session():
    return next(app.dependency_overrides[get_db]())


def _paired(client):  # noqa: F811
    """A front without a number, paired to a back that read #505 at 0.9."""
    front = _ingest_one(client, player="Pete Rose", year="1989", set_brand="Topps",
                        confidence=0.9)["cards"][0]
    _ingest_one(client, player="Pete Rose", year="1989", card_number="505", side="back",
                confidence=0.9,
                field_reads={"card_number": {"value": "505", "confidence": 0.9}})
    detail = client.get(f"/api/cards/{front['id']}").json()
    assert detail["card_number"] == "505" and detail["has_back"]
    return front["id"]


def _fake(monkeypatch, det):
    seen = {}

    def fake(crop_bytes, back_bytes=None):
        seen["back"] = back_bytes
        return det, "Claude"

    monkeypatch.setattr(vision, "reidentify_strongest", fake)
    return seen


def test_sends_both_sides_and_keeps_back_number_over_a_weaker_read(client, monkeypatch):  # noqa: F811
    card_id = _paired(client)
    seen = _fake(monkeypatch, DetectedCard(
        player="Pete Rose", year="1989", set_brand="Topps", card_number="50",
        confidence=0.8, field_reads={"card_number": FieldRead(value="50", confidence=0.5)},
    ))
    r = client.post(f"/api/cards/{card_id}/reanalyze")
    assert r.status_code == 200, r.text
    assert seen["back"]  # back crop went into the same request
    body = r.json()
    assert body["card_number"] == "505"
    audit = json.loads(body["identification_json"])
    assert audit["reanalysis"]["kept_back_fields"] == ["card_number"]
    assert audit["reanalysis"]["with_back"] is True


def test_surer_new_read_replaces_back_value(client, monkeypatch):  # noqa: F811
    card_id = _paired(client)
    _fake(monkeypatch, DetectedCard(
        player="Pete Rose", year="1989", card_number="506", confidence=0.95,
        field_reads={"card_number": FieldRead(value="506", confidence=0.97)},
    ))
    body = client.post(f"/api/cards/{card_id}/reanalyze").json()
    assert body["card_number"] == "506"
    assert body["set_brand"] == "Topps"  # empty in the new read: old value kept


def test_library_card_is_reanalyzed_in_place(client, monkeypatch):  # noqa: F811
    card_id = _paired(client)
    client.post("/api/cards/promote", json={"card_ids": [card_id]})
    _fake(monkeypatch, DetectedCard(player="Pete Rose", year="1989", set_brand="Topps",
                                    confidence=0.95))
    r = client.post(f"/api/cards/{card_id}/reanalyze")
    assert r.status_code == 200
    assert r.json()["status"] != "preview"


def test_listed_card_is_refused(client, monkeypatch):  # noqa: F811
    card_id = _paired(client)
    db = _session()
    db.add(Listing(card_id=card_id, ebay_mode="live", status="published", listing_id="1"))
    db.commit()
    _fake(monkeypatch, DetectedCard(player="X"))
    assert client.post(f"/api/cards/{card_id}/reanalyze").status_code == 409


def test_crop_reread_picks_the_central_card(monkeypatch):
    raw = json.dumps({"cards": [
        {"player": "Neighbour", "confidence": 0.9, "bbox": [0.0, 0.0, 0.15, 0.3]},
        {"player": "Middle", "confidence": 0.7, "bbox": [0.1, 0.05, 0.8, 0.9]},
        {"player": "Other edge", "confidence": 0.9, "bbox": [0.88, 0.6, 0.12, 0.4]},
    ]})
    monkeypatch.setattr(vision, "_generate", lambda *a, **k: raw)
    assert vision.reidentify(b"x").player == "Middle"


def test_crop_reread_without_box_covers_whole_crop(monkeypatch):
    monkeypatch.setattr(vision, "_generate", lambda *a, **k: '{"cards": [{"player": "A", "bbox": null}]}')
    det = vision.reidentify(b"x")
    assert det.bbox == [0.0, 0.0, 1.0, 1.0]


@pytest.mark.parametrize("back", [None, b"back"])
def test_reidentify_uses_pair_prompt_with_back(monkeypatch, back):
    from app.prompts.card_detection import CROP_USER, PAIR_USER

    seen = {}

    def fake(system, images, text, **kw):
        seen["images"], seen["text"] = images, text
        return '{"cards": [{"player": "A"}]}'

    monkeypatch.setattr(vision, "_generate", fake)
    vision.reidentify(b"front", back_bytes=back)
    if back:
        assert seen["images"] == [b"front", b"back"] and seen["text"] == PAIR_USER
    else:
        assert seen["images"] == b"front" and seen["text"] == CROP_USER


def test_back_supplied_fields_from_snapshot():
    from app.services.pairing import back_supplied_fields

    card = Card(card_number="505", year="1989", set_brand="Topps",
                pre_pair_identity_json=json.dumps({"card_number": None, "year": "1989",
                                                   "set_brand": "Topps"}),
                back_identification_json=json.dumps({"confidence": 0.8, "field_reads": {
                    "card_number": {"value": "505", "confidence": 0.9}}}))
    assert back_supplied_fields(card) == {"card_number": 0.9}
