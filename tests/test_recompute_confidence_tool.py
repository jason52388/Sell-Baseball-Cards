"""tools/recompute_paired_confidence: lift stuck paired cards, dry run first."""
import json

from app.models import Card, ImageUpload
from tools import recompute_paired_confidence as tool


def _reads(conf=None, **reads):
    audit = {"field_reads": {k: {"value": v[0], "confidence": v[1]} for k, v in reads.items()}}
    if conf is not None:
        audit["confidence"] = conf
    return json.dumps(audit)


def _paired(db, **kw):
    up = ImageUpload(filename="x.jpg")
    db.add(up)
    db.flush()
    defaults = dict(
        upload_id=up.id, side="front", status="needs_review",
        review_reason="low identification confidence",
        player="Pete Rose", year="1989", card_number="505", set_brand="Topps",
        confidence=0.6, back_crop_path="back.jpg",
        identification_json=_reads(player=("Pete Rose", 0.95), set_brand=("Topps", 0.8)),
        back_identification_json=_reads(
            0.85, player=("Pete Rose", 0.9), year=("1989", 0.9), card_number=("505", 0.9)),
    )
    defaults.update(kw)
    c = Card(**defaults)
    db.add(c)
    db.flush()
    return c


def test_dry_run_reports_but_writes_nothing(db_session):
    c = _paired(db_session)
    unpaired = _paired(db_session, back_crop_path=None)
    plan = tool.run(db_session, apply=False)
    assert [(p["card_id"], p["old"], p["new"]) for p in plan] == [(c.id, 0.6, 0.85)]
    assert c.confidence == 0.6 and c.pre_pair_identity_json is None
    assert unpaired.confidence == 0.6


def test_apply_raises_and_snapshots_the_old_confidence(db_session):
    c = _paired(db_session)
    tool.run(db_session, apply=True)
    assert c.confidence == 0.85
    assert json.loads(c.pre_pair_identity_json) == {"confidence": 0.6}


def test_apply_keeps_an_existing_snapshot_and_adds_confidence(db_session):
    snap = {"year": None, "card_number": None, "set_brand": "Topps"}
    c = _paired(db_session, pre_pair_identity_json=json.dumps(snap))
    tool.run(db_session, apply=True)
    saved = json.loads(c.pre_pair_identity_json)
    assert saved["confidence"] == 0.6 and saved["set_brand"] == "Topps"


def test_reprice_routes_library_cards_and_skips_listed(db_session, monkeypatch):
    c = _paired(db_session)
    listed = _paired(db_session, status="listed")
    priced = []
    monkeypatch.setattr(tool, "price_card", lambda card, db: priced.append(card.id))
    monkeypatch.setattr(tool, "preview_card", lambda card, db: priced.append(-card.id))
    tool.run(db_session, apply=True, reprice=True)
    assert priced == [c.id]
    assert listed.confidence == 0.85  # confidence still fixed, price left alone
