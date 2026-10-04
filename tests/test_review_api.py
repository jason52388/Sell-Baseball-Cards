"""Review queue, confirm, KPIs, and the listing fields on every card."""
import json

from app.config import get_settings
from app.db import get_db
from app.main import app
from app.models import Card, Listing
from app.services.ebay.listing_common import listing_price_floor, suggested_list_price
from tests.test_api import _ingest_one, client  # noqa: F401  (fixture)


def _session():
    return next(app.dependency_overrides[get_db]())


def _manual(client, **kw):  # noqa: F811
    body = {"player": "Ken Griffey Jr.", "year": "1989", "set_brand": "Upper Deck",
            "card_number": "1"}
    body.update(kw)
    return client.post("/api/cards/manual", json=body).json()


def _flag_for_review(card_id, confidence=0.5, reason="low identification confidence"):
    db = _session()
    c = db.get(Card, card_id)
    c.status = "needs_review"
    c.confidence = confidence
    c.review_reason = reason
    db.commit()


def test_card_out_carries_listing_state_and_list_price(client):  # noqa: F811
    card = _manual(client)
    s = get_settings()
    assert card["listing_state"] == "none"
    assert card["price_floor"] == listing_price_floor(s)
    db = _session()
    assert card["suggested_list_price"] == suggested_list_price(db.get(Card, card["id"]), s)
    assert card["suggested_list_price"] == 57.50
    db.add(Listing(card_id=card["id"], ebay_mode="live", status="published", list_price=57.99))
    db.commit()
    listed = next(c for c in client.get("/api/cards").json() if c["id"] == card["id"])
    assert listed["listing_state"] == "live"


def test_manual_entry_accepts_subset_team_and_rookie(client):  # noqa: F811
    card = _manual(client, subset="League Leaders", team="Mariners", rookie=True)
    assert card["subset"] == "League Leaders"
    assert card["team"] == "Mariners"
    assert card["rookie"] is True


def test_review_queue_walks_needs_review_cards_in_order(client):  # noqa: F811
    a, b, c = (_manual(client, card_number=str(n)) for n in (1, 2, 3))
    _flag_for_review(a["id"])
    _flag_for_review(c["id"])

    first = client.get("/api/review/next").json()
    assert first["card"]["id"] == a["id"] and first["remaining"] == 2
    assert first["review_reason"] == "low identification confidence"
    assert first["next_id"] == c["id"]
    assert first["front_crop_url"] is None  # manual card: no photo

    second = client.get(f"/api/review/next?after_id={a['id']}").json()
    assert second["card"]["id"] == c["id"]
    # Wraps to the start.
    assert client.get(f"/api/review/next?after_id={c['id']}").json()["card"]["id"] == a["id"]


def test_empty_review_queue(client):  # noqa: F811
    r = client.get("/api/review/next").json()
    assert r["card"] is None and r["remaining"] == 0


def test_review_fields_say_which_side_each_value_came_from(client):  # noqa: F811
    front = _ingest_one(
        client, player="Pete Rose", year="1989", set_brand="Topps", confidence=0.6,
        field_reads={"player": {"value": "Pete Rose", "confidence": 0.9},
                     "set_brand": {"value": "Topps", "confidence": 0.7}},
    )["cards"][0]
    _ingest_one(
        client, player="Pete Rose", year="1989", card_number="505", side="back",
        confidence=0.9,
        field_reads={"player": {"value": "Pete Rose", "confidence": 0.8},
                     "card_number": {"value": "505", "confidence": 0.95}},
    )
    client.post("/api/cards/promote", json={"card_ids": [front["id"]]})
    client.patch(f"/api/cards/{front['id']}", json={"team": "Reds"})
    _flag_for_review(front["id"], confidence=0.6)

    r = client.get("/api/review/next").json()
    fields = {f["field"]: f for f in r["fields"]}
    assert fields["player"]["side"] == "both" and fields["player"]["confidence"] == 0.9
    assert fields["set_brand"]["side"] == "front" and fields["set_brand"]["confidence"] == 0.7
    assert fields["card_number"]["side"] == "back" and fields["card_number"]["confidence"] == 0.95
    assert fields["team"]["side"] == "user" and fields["team"]["confidence"] == 1.0
    assert fields["card_number"]["back_read"] == {"value": "505", "confidence": 0.95}
    assert r["front_crop_url"] == f"/api/cards/{front['id']}/crop"
    assert r["back_crop_url"] == f"/api/cards/{front['id']}/back-crop"


def test_confirm_routes_a_priced_card_out_of_review(client):  # noqa: F811
    a, b = _manual(client, card_number="1"), _manual(client, card_number="2")
    _flag_for_review(a["id"])
    _flag_for_review(b["id"])
    r = client.post(f"/api/cards/{a['id']}/confirm")
    assert r.status_code == 200
    out = r.json()
    assert out["card"]["status"] == "priced"
    assert out["card"]["confidence"] == 1.0
    assert out["card"]["review_reason"] is None
    assert out["next_id"] == b["id"] and out["remaining"] == 1
    audit = json.loads(out["card"]["identification_json"])
    assert audit["user_confirmed"]["previous_confidence"] == 0.5
    # Nothing changed, so no correction row.
    from app.models import IdentificationCorrection
    assert _session().query(IdentificationCorrection).count() == 0


def test_confirm_keeps_price_reasons(client):  # noqa: F811
    a = _manual(client, psa10_candidate=True)
    assert a["status"] == "needs_review"
    _flag_for_review(a["id"], reason="low identification confidence; potential PSA 10: confirm grade before listing")
    out = client.post(f"/api/cards/{a['id']}/confirm").json()
    assert out["card"]["status"] == "needs_review"
    assert "PSA 10" in out["card"]["review_reason"]
    assert "confidence" not in out["card"]["review_reason"]


def test_confirm_prices_a_card_never_priced_for_low_confidence(client):  # noqa: F811
    a = _manual(client)
    db = _session()
    c = db.get(Card, a["id"])
    c.estimated_price = None
    db.commit()
    _flag_for_review(a["id"])
    out = client.post(f"/api/cards/{a['id']}/confirm").json()
    assert out["card"]["status"] == "priced" and out["card"]["estimated_price"] == 50.0


def test_editing_a_needs_review_library_card_keeps_it_in_the_library(client):  # noqa: F811
    a = _manual(client)
    _flag_for_review(a["id"])
    r = client.patch(f"/api/cards/{a['id']}", json={"card_number": "1"}).json()
    assert r["status"] == "priced"
    assert a["id"] in {c["id"] for c in client.get("/api/cards").json()}


def test_kpis_split_value_by_basis_and_count_the_workflow(client):  # noqa: F811
    a = _manual(client, card_number="1")
    b = _manual(client, card_number="2")
    c = _manual(client, card_number="3")
    d = _manual(client, card_number="4")
    db = _session()
    cb = db.get(Card, b["id"])
    cb.price_basis, cb.active_estimate = "active", 50.0
    db.add(Listing(card_id=c["id"], ebay_mode="live", status="published", list_price=60.0))
    from datetime import datetime, timezone
    db.add(Listing(card_id=d["id"], ebay_mode="live", status="sold", sold_price=55.0,
                   order_id="ORD1", sold_at=datetime.now(timezone.utc).replace(tzinfo=None)))
    db.commit()
    _flag_for_review(a["id"])

    s = client.get("/api/cards/stats").json()
    assert s["value_from_sold_count"] == 3 and s["value_from_sold"] == 150.0
    assert s["value_from_asking_count"] == 1 and s["value_from_asking"] == 50.0
    assert s["needs_review_count"] == 1
    assert s["ready_to_list_count"] == 1  # b: priced, not listed
    assert s["live_count"] == 1 and s["live_value"] == 60.0
    assert s["sold_count"] == 1 and s["sold_total"] == 55.0
    assert s["sold_this_month_count"] == 1 and s["sold_this_month_total"] == 55.0
    assert s["duplicates_count"] == 0
    assert "under_floor_count" in s and "below_threshold_count" in s
    assert s["price_floor"] == listing_price_floor(get_settings())
    # List value uses the listing rule (asking basis is undercut, not marked up).
    assert s["list_value_total"] > 0
