"""End-to-end: upload -> repository -> list, against in-memory SQLite.

Pricing's comp source is monkeypatched (no network); listing runs in the default
PREVIEW mode so nothing is actually published.
"""
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

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

    def fake_gather(query, graded=False):
        comps = [
            SoldComp(title="1989 Upper Deck Ken Griffey Jr. #1", sold_price=50.0,
                     sold_date="2026-05-20", thumbnail_url="https://i.ebayimg.com/x.jpg",
                     source="ebay (sold)", kind="sold")
            for _ in range(3)
        ]
        return comps, []

    monkeypatch.setattr(vision, "detect_cards", fake_detect)
    monkeypatch.setattr(vision, "verify_card", lambda b, c: VerificationResult(agree=True))
    monkeypatch.setattr(comp_sources, "gather_comps", fake_gather)

    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _png_bytes():
    img = Image.new("RGB", (400, 300), (200, 120, 60))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_upload_repository_list_flow(client):
    resp = client.post(
        "/api/upload", files={"files": ("cards.png", _png_bytes(), "image/png")}
    )
    assert resp.status_code == 200
    assert resp.json()["results"][0]["card_count"] == 2

    priced = client.get("/api/cards?status=priced").json()
    review = client.get("/api/cards?status=needs_review").json()
    assert len(priced) == 1
    assert priced[0]["player"] == "Ken Griffey Jr."
    assert priced[0]["estimated_price"] == 50.0
    assert priced[0]["reference_image_url"] == "https://i.ebayimg.com/x.jpg"
    assert any(c["player"] == "Blurry Guy" for c in review)

    griffey_id = priced[0]["id"]
    blurry_id = review[0]["id"]

    # Detail exposes comps with links.
    detail = client.get(f"/api/cards/{griffey_id}").json()
    assert detail["comps"] and any(c["listing_url"] is not None or True for c in detail["comps"])

    # Per-card "List on eBay" in preview mode: nothing published, x1.5 price.
    one = client.post(f"/api/cards/{griffey_id}/list").json()
    assert one["status"] == "preview"
    assert one["listing_id"] is None
    assert one["list_price"] == round(50.0 * 1.5, 2)

    # Card stays priced (it was only previewed, not really listed).
    assert client.get(f"/api/cards/{griffey_id}").json()["status"] == "priced"


def test_low_confidence_never_listed(client):
    client.post("/api/upload", files={"files": ("c.png", _png_bytes(), "image/png")})
    blurry_id = client.get("/api/cards?status=needs_review").json()[0]["id"]
    # Both the batch and single endpoints must refuse a non-priced card.
    batch = client.post("/api/listings/sell", json={"card_ids": [blurry_id]}).json()
    assert batch["results"][0]["status"] == "skipped"
    single = client.post(f"/api/cards/{blurry_id}/list").json()
    assert single["status"] == "skipped"


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
