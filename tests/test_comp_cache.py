"""Comp cache: writes share the caller's session, separate TTLs, no empties."""
import logging
from datetime import date, datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.models import Card, ImageUpload, PriceCache
from app.services import comp_cache
from app.services.ebay.base import SoldComp

MKT = "EBAY_US"
Q = "1989 Upper Deck Ken Griffey Jr. #1"


@pytest.fixture(autouse=True)
def no_private_sessions(monkeypatch):
    """The cache must never open its own session when handed the caller's:
    a second SQLite connection waits on the caller's write lock and fails
    with "database is locked"."""
    def refuse():
        raise AssertionError("comp_cache opened its own session")
    monkeypatch.setattr(comp_cache, "SessionLocal", refuse)
    s = get_settings()
    monkeypatch.setattr(s, "price_cache_ttl_days", 30)
    monkeypatch.setattr(s, "price_cache_active_ttl_days", 7)


def sold(price, day=None, source="ebay (sold)"):
    day = day or (date.today() - timedelta(days=3)).isoformat()
    return SoldComp(title=Q, sold_price=price, sold_date=day, source=source,
                    listing_url=f"https://e/{price}", kind="sold")


def active(price):
    return SoldComp(title=Q, sold_price=price, source="ebay (active)", kind="active")


def _age(db, days):
    row = db.query(PriceCache).one()
    row.fetched_at = datetime.now(timezone.utc) - timedelta(days=days)
    db.flush()


def test_round_trip_through_the_callers_session(db_session):
    comp_cache.put(Q, graded=False, marketplace=MKT, comps=[sold(10.0)], db=db_session)
    got = comp_cache.get(Q, graded=False, marketplace=MKT, db=db_session)
    assert got and got[0].sold_price == 10.0


def test_a_failed_write_is_logged_loudly_and_leaves_the_caller_usable(
    db_session, monkeypatch, caplog
):
    def boom(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(comp_cache, "merge_comps", boom)
    with caplog.at_level(logging.ERROR, logger="comp_cache"):
        comp_cache.put(Q, graded=False, marketplace=MKT, comps=[sold(10.0)], db=db_session)
    assert any(r.levelno >= logging.ERROR for r in caplog.records)
    up = ImageUpload(filename="x.jpg")
    db_session.add(up)
    db_session.flush()
    db_session.add(Card(upload_id=up.id, player="X"))
    db_session.flush()  # the caller's transaction still works


def test_empty_results_are_never_stored(db_session):
    comp_cache.put(Q, graded=False, marketplace=MKT, comps=[], db=db_session)
    assert db_session.query(PriceCache).count() == 0


def test_sold_only_entry_lives_for_the_sold_ttl(db_session):
    comp_cache.put(Q, graded=False, marketplace=MKT, comps=[sold(10.0)], db=db_session)
    _age(db_session, 20)
    assert comp_cache.get(Q, graded=False, marketplace=MKT, db=db_session)
    _age(db_session, 31)
    assert comp_cache.get(Q, graded=False, marketplace=MKT, db=db_session) is None


def test_asking_prices_expire_after_the_active_ttl(db_session):
    comps = [sold(10.0), active(15.0)]
    comp_cache.put(Q, graded=False, marketplace=MKT, comps=comps, db=db_session)
    _age(db_session, 6)
    assert comp_cache.get(Q, graded=False, marketplace=MKT, db=db_session)
    _age(db_session, 8)
    assert comp_cache.get(Q, graded=False, marketplace=MKT, db=db_session) is None


def test_market_average_expires_after_the_active_ttl(db_session):
    avg = SoldComp(title=Q, sold_price=12.0, sold_date=date.today().isoformat(),
                   source="sportscardspro", kind="sold")
    comp_cache.put(Q, graded=False, marketplace=MKT, comps=[avg], db=db_session)
    _age(db_session, 8)
    assert comp_cache.get(Q, graded=False, marketplace=MKT, db=db_session) is None


def test_market_averages_are_snapshots_not_accumulated_history():
    """The SportsCardsPro average carries its fetch date so recency applies,
    but each refresh replaces it: piling up daily averages would count one
    market price as many sales."""
    day1 = SoldComp(title=Q, sold_price=12.0, sold_date="2026-09-01",
                    source="sportscardspro", kind="sold", listing_url="https://scp/p")
    day2 = SoldComp(title=Q, sold_price=13.0, sold_date="2026-09-08",
                    source="sportscardspro", kind="sold", listing_url="https://scp/p")
    merged = comp_cache.merge_comps([day1], [day2], retention_days=0)
    assert [c.sold_price for c in merged] == [13.0]


def test_without_a_session_the_legacy_path_still_works(monkeypatch, db_session):
    """Callers that pass no session keep the old behaviour (own session)."""
    class Ctx:
        def __enter__(self):
            return db_session

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(comp_cache, "SessionLocal", lambda: Ctx())
    comp_cache.put(Q, graded=False, marketplace=MKT, comps=[sold(10.0)])
    assert comp_cache.get(Q, graded=False, marketplace=MKT)
