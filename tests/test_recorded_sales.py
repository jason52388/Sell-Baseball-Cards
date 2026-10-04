"""Sold sales recorded by hand (POST /api/cards/{id}/sold-sales) join pricing."""
from datetime import date, timedelta

from app.services import comp_sources
from tests.test_api import client  # noqa: F401  (fixture)
from tests.test_review_api import _manual


def _recent(days=5):
    return (date.today() - timedelta(days=days)).isoformat()


def test_recorded_sales_price_a_card_when_sources_find_nothing(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(comp_sources, "gather_comps", lambda q, graded=False, **kw: ([], []))
    card = _manual(client)
    assert card["estimated_price"] is None
    sales = [
        {"title": "1989 Upper Deck Ken Griffey Jr. #1 Rookie", "price": p, "date": _recent(),
         "url": f"https://www.ebay.com/itm/{n}"}
        for n, p in enumerate((10.0, 12.0, 14.0))
    ]
    r = client.post(f"/api/cards/{card['id']}/sold-sales", json={"sales": sales})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["estimated_price"] == 12.0
    assert out["price_basis"] == "sold"
    assert "ebay sold (looked up)" in out["price_sources"]
    # Re-posting the same sales does not double them.
    client.post(f"/api/cards/{card['id']}/sold-sales", json={"sales": sales})
    assert len(client.get(f"/api/cards/{card['id']}/sold-sales").json()) == 3


def test_recorded_wrong_card_sale_is_excluded(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(comp_sources, "gather_comps", lambda q, graded=False, **kw: ([], []))
    card = _manual(client)
    sales = [{"title": "1989 Upper Deck Ken Griffey Jr. #1", "price": 10.0, "date": _recent()},
             {"title": "2019 Topps Mike Trout #1", "price": 900.0, "date": _recent()}]
    out = client.post(f"/api/cards/{card['id']}/sold-sales", json={"sales": sales}).json()
    assert out["estimated_price"] == 10.0


def test_replace_drops_earlier_sales(client):  # noqa: F811
    card = _manual(client)
    one = [{"title": "1989 Upper Deck Ken Griffey Jr. #1", "price": 10.0, "date": _recent()}]
    two = [{"title": "1989 Upper Deck Ken Griffey Jr. #1", "price": 11.0, "date": _recent(2)}]
    client.post(f"/api/cards/{card['id']}/sold-sales", json={"sales": one})
    client.post(f"/api/cards/{card['id']}/sold-sales", json={"sales": two, "replace": True})
    got = client.get(f"/api/cards/{card['id']}/sold-sales").json()
    assert [s["price"] for s in got] == [11.0]


# --- Add-to-collection duplicate warning (POST /api/cards/promote/check) ---

def _queued(client, **kw):  # noqa: F811
    from app.db import get_db
    from app.main import app
    from app.models import Card
    card = _manual(client, **kw)
    db = next(app.dependency_overrides[get_db]())
    db.get(Card, card["id"]).status = "preview"
    db.commit()
    return card


def test_check_flags_a_queued_card_already_owned(client):  # noqa: F811
    owned = _manual(client)
    queued = _queued(client)
    other = _queued(client, card_number="2")
    r = client.post("/api/cards/promote/check", json={"card_ids": [queued["id"], other["id"]]}).json()
    assert [m["id"] for m in r["matches"]] == [queued["id"]]
    m = r["matches"][0]
    assert m["tier"] == "certain"
    assert m["others"] == [{"id": owned["id"], "title": "1989 Upper Deck Ken Griffey Jr. #1",
                            "in_collection": True}]


def test_check_flags_two_copies_in_the_same_add(client):  # noqa: F811
    a, b = _queued(client), _queued(client)
    r = client.post("/api/cards/promote/check", json={"card_ids": [a["id"], b["id"]]}).json()
    assert sorted(m["id"] for m in r["matches"]) == sorted([a["id"], b["id"]])
    assert all(not o["in_collection"] for m in r["matches"] for o in m["others"])


def test_check_is_quiet_for_a_new_card(client):  # noqa: F811
    _manual(client)
    q = _queued(client, card_number="99")
    assert client.post("/api/cards/promote/check", json={"card_ids": [q["id"]]}).json() == {"matches": []}
