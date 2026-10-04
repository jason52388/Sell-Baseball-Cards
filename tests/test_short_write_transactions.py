"""Slow work (comp fetches, vision) must never run inside an open write
transaction: a background job commits after every step."""
from datetime import date, timedelta

from app.models import Card, Comp, ImageUpload
from app.services.ebay.base import SoldComp
from app.services.pricing import price_card


def _card(db, **kw):
    up = ImageUpload(filename="t.jpg")
    db.add(up)
    db.flush()
    card = Card(upload_id=up.id, player="Ken Griffey Jr.", year="1989",
                set_brand="Upper Deck", card_number="1", confidence=0.9, **kw)
    db.add(card)
    db.flush()
    db.add(Comp(card_id=card.id, title="old", sold_price=1.0, source="x"))
    db.commit()
    return card


def test_fetches_run_before_any_write_and_commit_releases_the_lock(db_session, monkeypatch):
    card = _card(db_session, psa10_candidate=True)
    events: list[str] = []
    recent = (date.today() - timedelta(days=3)).isoformat()

    def fetch(query, graded=False):
        # Nothing pending or flushed yet: the old comps are still there.
        assert not db_session.deleted and not db_session.dirty
        events.append("graded" if graded else "raw")
        return [SoldComp(title="1989 Upper Deck Ken Griffey Jr. #1", sold_price=50.0,
                         sold_date=recent, source="ebay", kind="sold")] * 3

    real_commit = db_session.commit
    monkeypatch.setattr(db_session, "commit", lambda: (events.append("commit"), real_commit()))
    price_card(card, db_session, fetch, commit_after_fetch=True)
    assert events[:3] == ["raw", "graded", "commit"]
    assert card.estimated_price == 50.0
