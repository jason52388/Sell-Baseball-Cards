"""Nothing in the library is lost by accident: merging a card away as a back,
replacing a back, deleting a listed or sold card, and soft delete with undo."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import event

from app.config import get_settings
from app.db import get_db
from app.main import app
from app.models import STATUS_DELETED, Card, ImageUpload, Listing
from app.services import cropping, photo_archive, trash
from app.services.ebay import orders
from tests.test_api import _ingest_one, _png_bytes, client  # noqa: F401  (fixture)


def _session():
    return next(app.dependency_overrides[get_db]())


def _manual(client, **kw):  # noqa: F811
    body = {"player": "Ken Griffey Jr.", "year": "1989", "set_brand": "Upper Deck",
            "card_number": "1"}
    body.update(kw)
    r = client.post("/api/cards/manual", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _list_live(card_id, status="published"):
    db = _session()
    db.add(Listing(card_id=card_id, ebay_mode="live", status=status, list_price=57.99,
                   offer_id=f"o{card_id}", sku=f"S{card_id}"))
    db.commit()


# --- merging a card away as a back ------------------------------------------------


def test_mark_back_on_a_library_card_needs_confirm(client):  # noqa: F811
    card = _manual(client)
    r = client.post(f"/api/cards/{card['id']}/mark-back")
    assert r.status_code == 409 and "confirm=true" in r.json()["detail"]
    assert client.get(f"/api/cards/{card['id']}").json()["status"] == "priced"
    assert client.post(f"/api/cards/{card['id']}/mark-back?confirm=true").status_code == 200


@pytest.mark.parametrize("status", ["published", "sold"])
def test_a_card_on_ebay_is_never_merged_away(client, status):  # noqa: F811
    front = _manual(client, card_number="7")
    other = _manual(client, card_number="8")
    _list_live(other["id"], status)
    for url in (
        f"/api/cards/{other['id']}/mark-back?confirm=true",
        f"/api/cards/{front['id']}/attach-back/{other['id']}?confirm=true",
    ):
        r = client.post(url)
        assert r.status_code == 409, url
        assert "eBay" in r.json()["detail"]
    db = _session()
    assert db.get(Card, other["id"]) is not None
    assert len(db.get(Card, other["id"]).listings) == 1


def test_a_preview_back_attaches_without_confirm(client):  # noqa: F811
    front = _ingest_one(client, player="Pete Rose", year="1989", set_brand="Topps")["cards"][0]
    back = _ingest_one(client, player="Somebody", year="1975", side="back")
    db = _session()
    back_id = db.query(Card).filter(Card.side == "back").one().id
    r = client.post(f"/api/cards/{front['id']}/attach-back/{back_id}")
    assert r.status_code == 200 and r.json()["has_back"]
    assert back is not None


def test_attaching_a_new_back_keeps_the_old_one_as_an_orphan(client):  # noqa: F811
    front = _ingest_one(client, player="Pete Rose", year="1989", set_brand="Topps",
                        card_number="505")["cards"][0]
    _ingest_one(client, player="Pete Rose", year="1989", card_number="505", side="back")
    db = _session()
    first_back_path = db.get(Card, front["id"]).back_crop_path
    assert first_back_path and Path(first_back_path).exists()

    _ingest_one(client, player="Other Guy", year="1960", card_number="1", side="back")
    db = _session()
    new_back = db.query(Card).filter(Card.side == "back").one()
    r = client.post(f"/api/cards/{front['id']}/attach-back/{new_back.id}")
    assert r.status_code == 200

    db = _session()
    assert db.get(Card, front["id"]).back_crop_path == new_back.crop_path
    orphan = db.query(Card).filter(Card.side == "back").one()
    assert orphan.crop_path == first_back_path
    assert Path(first_back_path).exists(), "the old back image must not be deleted"


def test_detached_back_gets_its_own_upload_time_and_batch_back(client):  # noqa: F811
    front = _ingest_one(client, player="Pete Rose", year="1989", set_brand="Topps",
                        card_number="505")["cards"][0]
    db = _session()
    taken = datetime(2026, 5, 1, 12, 0, 0)
    back = Card(upload_id=None, side="back", status="preview", player="Pete Rose",
                year="1989", card_number="505", crop_path=None, photo_taken_at=taken,
                batch_tag="box 3")
    up = ImageUpload(filename="IMG_back.jpg", stored_name="IMG_back-1234.jpg")
    db.add(up)
    db.flush()
    back.upload_id = up.id
    back.crop_path = cropping.save_replacement_photo(_jpeg_bytes(), 999)
    db.add(back)
    db.commit()
    back_upload = up.id
    assert client.post(f"/api/cards/{front['id']}/attach-back/{back.id}").status_code == 200

    client.post(f"/api/cards/{front['id']}/detach-back")
    db = _session()
    orphan = db.query(Card).filter(Card.side == "back").one()
    assert orphan.upload_id == back_upload
    assert orphan.photo_taken_at == taken
    assert orphan.batch_tag == "box 3"


def _jpeg_bytes():
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (200, 280), (50, 60, 70)).save(buf, format="JPEG")
    return buf.getvalue()


# --- deleting ------------------------------------------------------------------------


def test_deleting_a_live_card_ends_its_listing_first(client, monkeypatch):  # noqa: F811
    card = _manual(client)
    _list_live(card["id"])
    ended = []

    def fake_end(db, c):
        ended.append(c.id)
        for row in c.listings:
            row.status = "ended"
        db.commit()
        return {"ended": True, "card_ids": [c.id], "message": "Ended the eBay listing."}

    monkeypatch.setattr(orders, "end_listing_for_card", fake_end)
    r = client.delete(f"/api/cards/{card['id']}")
    assert r.status_code == 200 and ended == [card["id"]]
    assert r.json()["message"] == "Ended the eBay listing."
    assert r.json()["status"] == "deleted"


def test_when_ebay_refuses_to_end_nothing_is_deleted(client, monkeypatch):  # noqa: F811
    card = _manual(client)
    _list_live(card["id"])

    def refuse(db, c):
        raise RuntimeError("eBay error 25002: offer not found")

    monkeypatch.setattr(orders, "end_listing_for_card", refuse)
    r = client.delete(f"/api/cards/{card['id']}")
    assert r.status_code == 502 and "25002" in r.json()["detail"]
    assert client.get(f"/api/cards/{card['id']}").json()["status"] == "priced"


def test_a_sold_card_needs_confirm_to_delete(client):  # noqa: F811
    card = _manual(client)
    _list_live(card["id"], status="sold")
    assert client.delete(f"/api/cards/{card['id']}").status_code == 409
    assert client.delete(f"/api/cards/{card['id']}?confirm=true").status_code == 200


def test_restore_puts_the_card_back_where_it_was(client):  # noqa: F811
    card = _manual(client)
    client.delete(f"/api/cards/{card['id']}")
    assert client.get("/api/cards").json() == []
    assert client.get("/api/cards/stats").json()["card_count"] == 0
    r = client.post(f"/api/cards/{card['id']}/restore")
    assert r.status_code == 200 and r.json()["status"] == "priced"
    assert [c["id"] for c in client.get("/api/cards").json()] == [card["id"]]
    assert client.post(f"/api/cards/{card['id']}/restore").status_code == 409


def test_restore_after_a_week_is_refused(client):  # noqa: F811
    card = _manual(client)
    client.delete(f"/api/cards/{card['id']}")
    db = _session()
    c = db.get(Card, card["id"])
    c.deleted_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=8)
    db.commit()
    assert client.post(f"/api/cards/{card['id']}/restore").status_code == 410


def test_purge_removes_old_deleted_cards_and_their_crops(db_session, tmp_path):
    up = ImageUpload(filename="x.jpg")
    db_session.add(up)
    db_session.flush()
    crop = tmp_path / "crop.jpg"
    crop.write_bytes(b"x")
    old = Card(upload_id=up.id, status=STATUS_DELETED, crop_path=str(crop),
               deleted_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=9))
    recent = Card(upload_id=up.id, status=STATUS_DELETED,
                  deleted_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1))
    db_session.add_all([old, recent])
    db_session.commit()
    assert trash.purge_expired(db_session) == 1
    assert not crop.exists()
    assert db_session.query(Card).count() == 1


def test_a_deleted_back_never_pairs(client):  # noqa: F811
    _ingest_one(client, player="Pete Rose", year="1989", card_number="505", side="back")
    db = _session()
    back = db.query(Card).filter(Card.side == "back").one()
    client.delete(f"/api/cards/{back.id}")
    front = _ingest_one(client, player="Pete Rose", year="1989", set_brand="Topps",
                        card_number="505")["cards"][0]
    assert front["has_back"] is False


# --- promote + archive -----------------------------------------------------------------


def test_promote_reports_what_it_skipped(client):  # noqa: F811
    lib = _manual(client)
    r = client.post("/api/cards/promote", json={"card_ids": [lib["id"], 424242]}).json()
    assert r["added"] == []
    reasons = {s["id"]: s["reason"] for s in r["skipped"]}
    assert "already in the collection" in reasons[lib["id"]]
    assert reasons[424242] == "not found"


def test_a_multi_card_photo_archives_once_the_last_card_is_added(client, monkeypatch, tmp_path):  # noqa: F811
    dest = tmp_path / "collection"
    monkeypatch.setattr(get_settings(), "collection_photos_dir", str(dest))
    job = client.post(
        "/api/upload",
        files={"files": ("IMG_0042.png", _png_bytes(), "image/png")},
        data={"batch_tag": "box 7"},
    ).json()
    a, b = job["photos"][0]["card_ids"]
    up = _session().get(ImageUpload, job["photos"][0]["upload_id"])
    source = photo_archive.INBOX_PROCESSED_DIR / up.stored_name
    assert source.exists()

    client.post("/api/cards/promote", json={"card_ids": [a]})
    assert source.exists(), "one card added, one still queued: the photo stays"
    assert not list(dest.glob("*IMG_0042*"))

    client.post("/api/cards/promote", json={"card_ids": [b]})
    assert not source.exists()
    archived = [p.name for p in dest.glob("*IMG_0042*")]
    assert archived == ["box 7 IMG_0042.png"], archived


def test_discarding_the_last_queued_card_archives_the_photo(client, monkeypatch, tmp_path):  # noqa: F811
    dest = tmp_path / "collection"
    monkeypatch.setattr(get_settings(), "collection_photos_dir", str(dest))
    job = client.post(
        "/api/upload", files={"files": ("IMG_7.png", _png_bytes(), "image/png")},
    ).json()
    a, b = job["photos"][0]["card_ids"]
    client.post("/api/cards/promote", json={"card_ids": [a]})
    assert not list(dest.glob("*IMG_7*"))
    client.delete(f"/api/cards/{b}")
    assert len(list(dest.glob("*IMG_7*"))) == 1


# --- efficiency -------------------------------------------------------------------------


def test_duplicates_endpoint_loads_listings_in_one_query(client):  # noqa: F811
    for _ in range(4):
        _manual(client)
    db = _session()
    engine = db.get_bind()
    count = {"n": 0}

    def before(*a, **k):
        count["n"] += 1

    event.listen(engine, "before_cursor_execute", before)
    try:
        r = client.get("/api/cards/duplicates")
    finally:
        event.remove(engine, "before_cursor_execute", before)
    assert r.status_code == 200 and r.json()["groups"]
    assert count["n"] <= 3, count["n"]
