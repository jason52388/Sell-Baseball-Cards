"""End-to-end: upload -> repository -> list, against in-memory SQLite.

Pricing's comp source is monkeypatched (no network); listing runs in the default
PREVIEW mode so nothing is actually published.
"""
import io
import json
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.db import Base, get_db
from app.main import app
from app.schemas import DetectedCard, VerificationResult
from app.services import comp_sources, vision
from app.services.ebay.base import SoldComp


@pytest.fixture
def client(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    def override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db

    def fake_detect(image_bytes):
        return [
            DetectedCard(
                player="Ken Griffey Jr.", year="1989", set_brand="Upper Deck",
                card_number="1", confidence=0.95, bbox=[0.0, 0.0, 0.5, 0.5],
            ),
            DetectedCard(player="Blurry Guy", confidence=0.2, bbox=[0.5, 0.5, 0.4, 0.4]),
        ]

    def fake_gather(query, graded=False, **kw):
        # Dated relative to today: a hardcoded date silently ages out of the
        # comp recency window and turns the whole suite red months later.
        recent = (date.today() - timedelta(days=7)).isoformat()
        comps = [
            SoldComp(title="1989 Upper Deck Ken Griffey Jr. #1", sold_price=50.0,
                     sold_date=recent, thumbnail_url="https://i.ebayimg.com/x.jpg",
                     source="ebay (sold)", kind="sold")
            for _ in range(3)
        ]
        return comps, []

    monkeypatch.setattr(vision, "detect_cards", fake_detect)
    monkeypatch.setattr(vision, "verify_card", lambda b, c: VerificationResult(agree=True))
    monkeypatch.setattr(comp_sources, "gather_comps", fake_gather)
    # Keep tests hermetic regardless of the developer's .env: never download
    # reference images, and exercise the eBay listing flow in preview mode.
    monkeypatch.setattr(get_settings(), "localize_reference_images", False)
    monkeypatch.setattr(get_settings(), "ebay_mode", "preview")

    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _png_bytes():
    img = Image.new("RGB", (400, 300), (200, 120, 60))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _upload_two(client):
    """Upload the fixture image and return (griffey_preview, blurry_preview)."""
    resp = client.post(
        "/api/upload", files={"files": ("cards.png", _png_bytes(), "image/png")}
    )
    assert resp.status_code == 200
    cards = resp.json()["results"][0]["cards"]
    assert resp.json()["results"][0]["card_count"] == 2
    griffey = next(c for c in cards if c["player"] == "Ken Griffey Jr.")
    blurry = next(c for c in cards if c["player"] == "Blurry Guy")
    return griffey, blurry


def test_upload_previews_then_promote_flow(client):
    griffey, blurry = _upload_two(client)

    # Upload only produces PREVIEW cards — nothing is in the repository yet.
    assert griffey["status"] == "preview"
    assert client.get("/api/cards").json() == []
    # Even low-confidence previews still get a marketplace reference photo so the
    # user can verify the match before adding.
    assert griffey["reference_image_url"] == "https://i.ebayimg.com/x.jpg"
    # Tentative estimate is shown at preview time.
    assert griffey["estimated_price"] == 50.0

    # Promote both: high-confidence -> priced, low-confidence -> needs_review.
    promoted = client.post(
        "/api/cards/promote", json={"card_ids": [griffey["id"], blurry["id"]]}
    ).json()
    by_id = {c["id"]: c for c in promoted}
    assert by_id[griffey["id"]]["status"] == "priced"
    assert by_id[griffey["id"]]["estimated_price"] == 50.0
    assert by_id[blurry["id"]]["status"] == "needs_review"

    # Now they appear in the (default, preview-excluding) repository listing.
    assert {c["id"] for c in client.get("/api/cards").json()} == {
        griffey["id"], blurry["id"]
    }

    # Detail exposes comps with links.
    detail = client.get(f"/api/cards/{griffey['id']}").json()
    assert detail["comps"]

    # Per-card "List on eBay" in preview mode: nothing published, x1.5 price.
    one = client.post(f"/api/cards/{griffey['id']}/list").json()
    assert one["status"] == "preview"
    assert one["listing_id"] is None
    assert one["list_price"] == round(50.0 * 1.5, 2)
    assert client.get(f"/api/cards/{griffey['id']}").json()["status"] == "priced"


def test_delete_card_preview_or_library(client):
    griffey, blurry = _upload_two(client)
    # A preview card can be deleted.
    assert client.delete(f"/api/cards/{blurry['id']}").status_code == 204
    assert client.get(f"/api/cards/{blurry['id']}").status_code == 404
    # A promoted (library) card can also be deleted now.
    client.post("/api/cards/promote", json={"card_ids": [griffey["id"]]})
    assert client.delete(f"/api/cards/{griffey['id']}").status_code == 204
    assert client.get(f"/api/cards/{griffey['id']}").status_code == 404
    # Deleting a missing card is a 404.
    assert client.delete("/api/cards/999999").status_code == 404


def test_low_confidence_never_listed(client):
    _, blurry = _upload_two(client)
    blurry_id = client.post(
        "/api/cards/promote", json={"card_ids": [blurry["id"]]}
    ).json()[0]["id"]
    # Both the batch and single endpoints must refuse a non-priced card.
    batch = client.post("/api/listings/sell", json={"card_ids": [blurry_id]}).json()
    assert batch["results"][0]["status"] == "skipped"
    single = client.post(f"/api/cards/{blurry_id}/list").json()
    assert single["status"] == "skipped"


def test_sell_set_combines_cards_into_one_listing(client):
    # Two priced cards (manual entry prices them to 50.0 via the fake comps).
    a = client.post("/api/cards/manual", json={
        "player": "Ken Griffey Jr.", "year": "1989", "set_brand": "Upper Deck",
        "card_number": "1"}).json()
    b = client.post("/api/cards/manual", json={
        "player": "Ken Griffey Jr.", "year": "1989", "set_brand": "Upper Deck",
        "card_number": "2"}).json()
    assert a["status"] == "priced" and b["status"] == "priced"

    r = client.post("/api/listings/sell-set", json={"card_ids": [a["id"], b["id"]]}).json()
    assert r["status"] == "preview"  # default preview mode publishes nothing
    assert sorted(r["card_ids"]) == sorted([a["id"], b["id"]])
    # Lot price is the sum of each card's individual list price (estimate x1.5).
    assert r["list_price"] == round(50.0 * 1.5 * 2, 2)
    assert r["sku"].startswith("SET-")

    # A non-sellable card is skipped, not fatal to the whole lot.
    mixed = client.post(
        "/api/listings/sell-set", json={"card_ids": [a["id"], 999999]}
    ).json()
    assert mixed["card_ids"] == [a["id"]]
    assert any("999999" in s for s in mixed["skipped"])

    # An all-unsellable selection returns skipped, not a crash.
    none = client.post("/api/listings/sell-set", json={"card_ids": [999999]}).json()
    assert none["status"] == "skipped" and none["card_ids"] == []


def test_manual_card_entry_prices_without_vision(client):
    """Manual entry needs no Anthropic key; it prices from comps like any card."""
    resp = client.post("/api/cards/manual", json={
        "player": "Ken Griffey Jr.", "year": "1989",
        "set_brand": "Upper Deck", "card_number": "1", "condition": "near-mint",
    })
    assert resp.status_code == 200
    card = resp.json()
    assert card["estimated_price"] == 50.0
    assert card["status"] == "priced"
    assert card["confidence"] == 1.0


def test_manual_card_requires_player(client):
    resp = client.post("/api/cards/manual", json={"year": "1989"})
    assert resp.status_code == 422


def test_pair_two_cards_regardless_of_side(client):
    """The manual matcher can pair ANY two cards — even when both are labelled
    'front' (the AI often mislabels a back as a front)."""
    a = client.post("/api/cards/manual", json={
        "player": "Kerry Wood", "year": "2001", "set_brand": "Topps", "card_number": "623",
    }).json()
    b = client.post("/api/cards/manual", json={
        "player": "Kerry Wood", "year": "2001", "set_brand": "Topps", "card_number": "786",
    }).json()
    # Both are fronts; the old attach-back rejected this. /pair must accept it.
    r = client.post(f"/api/cards/{a['id']}/pair/{b['id']}")
    assert r.status_code == 200
    assert r.json()["id"] == a["id"]  # the more front-like card survives
    # The second card was absorbed as the back and removed.
    assert client.get(f"/api/cards/{b['id']}").status_code == 404
    assert client.get(f"/api/cards/{a['id']}").status_code == 200


def test_pair_requires_two_different_cards(client):
    a = client.post("/api/cards/manual", json={"player": "X", "year": "2001"}).json()
    assert client.post(f"/api/cards/{a['id']}/pair/{a['id']}").status_code == 422


def test_detach_back_splits_the_pair_again(client):
    # Use uploaded cards (they have crops, unlike manual entries).
    griffey, blurry = _upload_two(client)
    front = client.post(f"/api/cards/{griffey['id']}/pair/{blurry['id']}").json()
    assert front["has_back"] is True
    # Now detach — a standalone back should reappear and the front lose its back.
    r = client.post(f"/api/cards/{front['id']}/detach-back")
    assert r.status_code == 200
    assert r.json()["has_back"] is False
    backs = client.get("/api/cards?status=unmatched_backs").json()
    assert any(bk["side"] == "back" for bk in backs)


def test_unmatching_a_wrong_back_restores_the_fronts_own_identity(client):
    """A manually attached back overwrites the front's card number. Undoing the
    match must undo that too, or the front keeps the wrong card's identity."""
    front = _ingest_one(client, player="Kerry Wood", year="2001",
                        set_brand="Topps", card_number="12")["cards"][0]
    wrong_back = _ingest_one(client, player="Kerry Wood", year="2001",
                             set_brand="Topps", card_number="47")["cards"][0]

    paired = client.post(
        f"/api/cards/{front['id']}/attach-back/{wrong_back['id']}"
    ).json()
    assert paired["card_number"] == "47"  # back is authoritative while attached

    detached = client.post(f"/api/cards/{front['id']}/detach-back").json()
    assert detached["card_number"] == "12", "the front's own number must come back"


def test_detach_requires_a_back(client):
    a = client.post("/api/cards/manual", json={"player": "Solo", "year": "2001"}).json()
    assert client.post(f"/api/cards/{a['id']}/detach-back").status_code == 422


def test_collection_stats(client):
    for _ in range(2):
        client.post("/api/cards/manual", json={
            "player": "Ken Griffey Jr.", "year": "1989",
            "set_brand": "Upper Deck", "card_number": "1",
        })
    s = client.get("/api/cards/stats").json()
    assert s["card_count"] == 2
    assert s["priced_count"] == 2
    assert s["total_value"] == 100.0          # 2 x $50
    assert s["selling_expenses"] > 0
    assert s["listed_count"] == 0 and s["active_listings_value"] == 0


def test_reprice_refetches_library_cards(client):
    client.post("/api/cards/manual", json={"player": "A", "year": "2001"})
    client.post("/api/cards/manual", json={"player": "B", "year": "2001"})
    r = client.post("/api/cards/reprice")
    assert r.status_code == 200
    assert r.json()["repriced"] == 2 and r.json()["total"] == 2


def test_ingest_creates_previews_without_vision(client, monkeypatch):
    """Externally-identified cards POSTed to /api/ingest are cropped + priced as
    previews — using NO vision API (detect_cards is made to raise if called)."""
    def boom(*a, **k):  # pragma: no cover - asserts ingest never calls vision
        raise AssertionError("ingest must not call the vision model")

    monkeypatch.setattr(vision, "detect_cards", boom)
    monkeypatch.setattr(vision, "verify_card", boom)

    detections = json.dumps({"cards": [{
        "player": "Ken Griffey Jr.", "year": "1989", "set_brand": "Upper Deck",
        "card_number": "1", "confidence": 0.95, "bbox": [0.0, 0.0, 0.5, 0.5],
    }]})
    resp = client.post(
        "/api/ingest",
        files={"image": ("cards.png", _png_bytes(), "image/png")},
        data={"detections": detections},
    )
    assert resp.status_code == 200
    cards = resp.json()["cards"]
    assert len(cards) == 1
    c = cards[0]
    assert c["status"] == "preview"
    assert c["player"] == "Ken Griffey Jr."
    assert c["crop_path"]  # server cropped from the original image
    assert c["reference_image_url"] == "https://i.ebayimg.com/x.jpg"

    # Not in the repository until promoted; then it routes normally.
    assert client.get("/api/cards").json() == []
    promoted = client.post("/api/cards/promote", json={"card_ids": [c["id"]]}).json()
    assert promoted[0]["status"] == "priced"
    assert promoted[0]["estimated_price"] == 50.0


def _ingest_one(client, **fields):
    """Ingest a single externally-identified detection and return the card dict."""
    det = {"confidence": 0.95, "bbox": [0.0, 0.0, 0.5, 0.5]}
    det.update(fields)
    resp = client.post(
        "/api/ingest",
        files={"image": ("cards.png", _png_bytes(), "image/png")},
        data={"detections": json.dumps({"cards": [det]})},
    )
    assert resp.status_code == 200
    return resp.json()


def test_uploading_a_back_does_not_demote_a_promoted_card(client):
    """Fronts first, backs later is the normal workflow: pairing a back must not
    knock the front out of the collection or wipe its price."""
    front = _ingest_one(
        client, player="Ken Griffey Jr.", year="1989", set_brand="Upper Deck",
        card_number="1", side="front",
    )["cards"][0]
    promoted = client.post(
        "/api/cards/promote", json={"card_ids": [front["id"]]}
    ).json()[0]
    assert promoted["status"] == "priced"

    # Now the back of that same card arrives in a later upload.
    _ingest_one(
        client, player="Ken Griffey Jr.", year="1989", set_brand="Upper Deck",
        card_number="1", side="back",
    )

    after = client.get(f"/api/cards/{front['id']}").json()
    assert after["has_back"] is True, "the back should still attach to its front"
    assert after["status"] == "priced", "a promoted card must not fall back to preview"
    assert after["estimated_price"] == 50.0
    # And it is still visible in the collection listing.
    assert front["id"] in {c["id"] for c in client.get("/api/cards").json()}


def test_editing_a_low_confidence_card_reprices_instead_of_dropping_its_comps(client):
    """Correcting a flagged card's identity is the intended fix path: it must
    re-price from the corrected identity, not delete the evidence and give up."""
    _, blurry = _upload_two(client)
    promoted = client.post(
        "/api/cards/promote", json={"card_ids": [blurry["id"]]}
    ).json()[0]
    assert promoted["status"] == "needs_review"

    fixed = client.patch(f"/api/cards/{blurry['id']}", json={
        "player": "Ken Griffey Jr.", "year": "1989",
        "set_brand": "Upper Deck", "card_number": "1",
    })
    assert fixed.status_code == 200
    card = fixed.json()
    assert card["estimated_price"] == 50.0, "corrected card should price from comps"
    assert card["status"] == "priced"
    assert client.get(f"/api/cards/{blurry['id']}").json()["comps"], "comps kept"


def test_replace_photo_rejects_a_non_image(client):
    """Crops are served publicly at /crops and the app is internet-reachable
    while the listing tunnel is up, so only real images may land there."""
    griffey, _ = _upload_two(client)
    resp = client.post(
        f"/api/cards/{griffey['id']}/replace-photo?side=front",
        files={"file": ("evil.html", b"<script>alert(1)</script>", "text/html")},
    )
    assert resp.status_code == 422


def test_replace_photo_accepts_a_real_image(client):
    griffey, _ = _upload_two(client)
    resp = client.post(
        f"/api/cards/{griffey['id']}/replace-photo?side=front",
        files={"file": ("new.png", _png_bytes(), "image/png")},
    )
    assert resp.status_code == 200
    assert resp.json()["crop_path"].endswith(".jpg")


def test_ingest_rejects_bad_json(client):
    resp = client.post(
        "/api/ingest",
        files={"image": ("c.png", _png_bytes(), "image/png")},
        data={"detections": "not json at all"},
    )
    assert resp.status_code == 422


def test_queue_saves_photos_to_inbox_without_ai(client, monkeypatch, tmp_path):
    """The 'Queue for Claude' button drops photos into the inbox with no AI call."""
    from app.routers import upload as upload_router

    monkeypatch.setattr(upload_router, "INBOX_DIR", tmp_path)
    monkeypatch.setattr(
        vision, "detect_cards",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("queue must not call AI")),
    )

    resp = client.post(
        "/api/queue",
        files=[
            ("files", ("a.png", _png_bytes(), "image/png")),
            ("files", ("b.png", _png_bytes(), "image/png")),
        ],
    )
    assert resp.status_code == 200
    assert resp.json()["queued"] == 2
    saved = list(tmp_path.glob("*.png"))
    assert len(saved) == 2 and all(p.read_bytes() for p in saved)
    # Queueing only stages files — nothing enters the repository yet.
    assert client.get("/api/cards").json() == []


def test_duplicates_endpoint_groups_identical_library_cards(client):
    a = client.post("/api/cards/manual", json={
        "player": "Barry Bonds", "year": "2001", "set_brand": "Topps",
        "card_number": "497"}).json()
    b = client.post("/api/cards/manual", json={
        "player": "Barry Bonds", "year": "2001", "set_brand": "Topps",
        "card_number": "497"}).json()
    # Same player and set but a different card: must not be grouped with them.
    client.post("/api/cards/manual", json={
        "player": "Barry Bonds", "year": "2001", "set_brand": "Topps",
        "card_number": "250"})

    resp = client.get("/api/cards/duplicates")
    assert resp.status_code == 200
    groups = resp.json()["groups"]
    assert len(groups) == 1
    assert groups[0]["tier"] == "certain"
    assert sorted(c["id"] for c in groups[0]["cards"]) == sorted([a["id"], b["id"]])
    assert "Barry Bonds" in groups[0]["label"]


def test_duplicates_endpoint_is_empty_for_a_clean_library(client):
    client.post("/api/cards/manual", json={"player": "Solo Guy", "year": "2001",
                                           "set_brand": "Topps", "card_number": "1"})
    assert client.get("/api/cards/duplicates").json()["groups"] == []


def test_duplicates_route_is_not_shadowed_by_the_card_id_route(client):
    """'/api/cards/duplicates' must not be parsed as a card id."""
    assert client.get("/api/cards/duplicates").status_code == 200
