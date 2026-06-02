"""Vision JSON parsing: clean, fenced, malformed, and verification."""
import pytest

from app.services import vision


def test_parse_plain_json():
    raw = '{"cards": [{"player": "Ken Griffey Jr.", "year": "1989", "confidence": 0.9, "bbox": [0,0,0.3,0.5]}]}'
    cards = vision.parse_detection(raw)
    assert len(cards) == 1
    assert cards[0].player == "Ken Griffey Jr."
    assert cards[0].confidence == 0.9


def test_parse_fenced_json():
    raw = "```json\n{\"cards\": [{\"player\": \"Mike Trout\", \"confidence\": 0.8}]}\n```"
    cards = vision.parse_detection(raw)
    assert cards[0].player == "Mike Trout"


def test_parse_bare_array():
    raw = '[{"player": "A", "confidence": 0.5}, {"player": "B", "confidence": 0.6}]'
    cards = vision.parse_detection(raw)
    assert len(cards) == 2


def test_parse_malformed_raises():
    with pytest.raises(Exception):
        vision.parse_detection("not json at all")


def test_field_reads_and_flags():
    raw = """{"cards": [{
        "player": "Mickey Mantle", "year": "1952", "set_brand": "Topps",
        "confidence": 0.95, "bbox": [0,0,0.5,0.5],
        "psa10_candidate": true, "gem_mint_score": 0.92,
        "anomaly_flag": true, "anomaly_notes": "off-center miscut",
        "raw_text": "MANTLE 1952 TOPPS",
        "field_reads": {"player": {"value": "Mickey Mantle", "confidence": 0.97}}
    }]}"""
    card = vision.parse_detection(raw)[0]
    assert card.psa10_candidate is True
    assert card.anomaly_flag is True
    assert card.field_reads["player"].confidence == 0.97
    assert card.raw_text == "MANTLE 1952 TOPPS"


def test_parse_verification():
    raw = '{"agree": false, "corrections": {"year": "1990"}, "notes": "year misread"}'
    v = vision.parse_verification(raw)
    assert v.agree is False
    assert v.corrections["year"] == "1990"


def test_salvage_truncated_detection():
    # Two complete cards then a third object cut off mid-way (token limit).
    truncated = (
        '{"cards": ['
        '{"player": "Ken Griffey Jr.", "year": "1989", "confidence": 0.9},'
        '{"player": "Mike Trout", "year": "2011", "confidence": 0.8},'
        '{"player": "Juan Soto", "year": "2018", "confiden'
    )
    cards = vision.parse_detection(truncated)
    assert [c.player for c in cards] == ["Ken Griffey Jr.", "Mike Trout"]


def test_unrecoverable_json_raises():
    import pytest
    with pytest.raises(Exception):
        vision.parse_detection("total garbage, no json")
