"""The real Sell API client and listing endpoints, with eBay mocked at the
HTTP layer (httpx.MockTransport). Nothing here can reach eBay."""
import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.db import Base, get_db
from app.main import app
from app.models import Card, ImageUpload, Listing
from app.services.ebay import listing_common, media, oauth, orders, sandbox
from app.services.ebay.sandbox import SandboxEbayClient

EPS = "https://i.ebayimg.com/images/g/"


class FakeEbay:
    """Records every request and answers like eBay would."""

    def __init__(self):
        self.calls: list[tuple[str, str, object]] = []
        self.existing_offers: list[dict] = []
        self.publish_status = 200
        self.upload_status = 201
        self.uploads = 0
        self.head_status = 200
        self.head_type = "image/jpeg"
        self.orders: list[dict] = []
        self.offer_body = {}
        self.item_body = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        body = None
        if request.headers.get("content-type", "").startswith("application/json") and request.content:
            body = json.loads(request.content)
        self.calls.append((method, path, body))
        if path.endswith("/image/create_image_from_file"):
            self.uploads += 1
            if self.upload_status >= 400:
                return httpx.Response(self.upload_status, json={"errors": [{"longMessage": "media down"}]})
            return httpx.Response(201, json={"imageUrl": f"{EPS}{self.uploads}.jpg",
                                             "expirationDate": "2099-01-01T00:00:00Z"})
        if method == "PUT" and "/inventory_item/" in path:
            return httpx.Response(204)
        if method == "GET" and "/inventory_item/" in path:
            return httpx.Response(200, json=self.item_body)
        if method == "GET" and path == "/sell/inventory/v1/offer":
            return httpx.Response(200, json={"offers": self.existing_offers})
        if method == "POST" and path == "/sell/inventory/v1/offer":
            return httpx.Response(201, json={"offerId": "OFF-NEW"})
        if method == "PUT" and "/offer/" in path:
            return httpx.Response(204)
        if method == "GET" and "/sell/inventory/v1/offer/" in path:
            return httpx.Response(200, json=self.offer_body)
        if path.endswith("/publish"):
            if self.publish_status >= 400:
                return httpx.Response(self.publish_status, json={"errors": [
                    {"longMessage": "The item specific Number of Cards is missing."}]})
            return httpx.Response(200, json={"listingId": "1234567890"})
        if path.endswith("/withdraw"):
            return httpx.Response(200, json={"listingId": "1234567890"})
        if method == "DELETE":
            return httpx.Response(204)
        if path == "/sell/fulfillment/v1/order":
            return httpx.Response(200, json={"orders": self.orders, "total": len(self.orders)})
        return httpx.Response(404, json={"errors": [{"longMessage": f"unmocked {method} {path}"}]})


@pytest.fixture
def ebay(monkeypatch, tmp_path):
    fake = FakeEbay()
    real_client = httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        return real_client(*args, **kwargs)

    def fake_head(url, **kw):
        fake.calls.append(("HEAD", url, None))
        return httpx.Response(fake.head_status, headers={"content-type": fake.head_type})

    def no_post(*a, **k):
        raise AssertionError("unexpected direct httpx.post (token call not mocked)")

    monkeypatch.setattr(httpx, "Client", client_factory)
    monkeypatch.setattr(httpx, "head", fake_head)
    monkeypatch.setattr(httpx, "post", no_post)
    monkeypatch.setattr(sandbox, "get_user_access_token", lambda live=False, scope=None: "user-token")
    monkeypatch.setattr(orders, "get_user_access_token", lambda live=False, scope=None: "ful-token")
    s = get_settings()
    for key, val in {
        "ebay_client_id": "cid", "ebay_client_secret": "sec", "ebay_user_refresh_token": "rt",
        "ebay_fulfillment_policy_id": "F", "ebay_payment_policy_id": "P",
        "ebay_return_policy_id": "R", "ebay_merchant_location_key": "LOC",
        "public_image_base_url": "https://tunnel.example.com", "ebay_mode": "live",
        "ebay_include_reference_image": False, "ebay_upload_images": True,
        "price_markup": 1.15, "ebay_ask_undercut": 0.95,
        "ebay_envelope_fulfillment_policy_id": "",
    }.items():
        monkeypatch.setattr(s, key, val)
    oauth.clear_token_cache()
    return fake


def _photo(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"\xff\xd8\xff fake jpeg")
    return str(p)


def make_card(tmp_path, **kw):
    from types import SimpleNamespace

    base = dict(id=7, year="1989", set_brand="Upper Deck", player="Ken Griffey Jr.",
                card_number="1", parallel=None, serial_number=None, condition="near-mint",
                crop_path=_photo(tmp_path, "7-front.jpg"), back_crop_path=_photo(tmp_path, "7-back.jpg"),
                reference_image_url="https://i.ebayimg.com/someone-else.jpg",
                sport="baseball", estimated_price=50.0, price_basis="sold")
    base.update(kw)
    return SimpleNamespace(**base)


def _bodies(fake, method, fragment):
    return [b for m, p, b in fake.calls if m == method and fragment in p]


# --- the real client's payload --------------------------------------------------

def test_real_payload_matches_the_shared_builder_with_eps_photos(ebay, tmp_path):
    card = make_card(tmp_path)
    r = SandboxEbayClient(live=True).create_listing(card, 57.99)
    assert r.status == "published" and r.listing_id == "1234567890"

    inv = _bodies(ebay, "PUT", "/inventory_item/CARD-7")[0]
    offer = _bodies(ebay, "POST", "/sell/inventory/v1/offer")[0]
    # Front and back, both hosted by eBay; never the other seller's photo.
    assert inv["product"]["imageUrls"] == [f"{EPS}1.jpg", f"{EPS}2.jpg"]
    expected = listing_common.build_single_payload(card, 57.99, get_settings(), inv["product"]["imageUrls"])
    assert inv == expected["inventory_item"]
    assert offer == expected["offer"]
    assert inv["condition"] == "USED_VERY_GOOD"
    assert offer["listingPolicies"]["bestOfferTerms"]["autoAcceptPrice"]["value"] == "46.39"
    # No tunnel check needed when eBay hosts the photos.
    assert not [c for c in ebay.calls if c[0] == "HEAD"]


def test_photo_uploads_are_cached_across_retries(ebay, tmp_path):
    card = make_card(tmp_path)
    SandboxEbayClient(live=True).create_listing(card, 57.99)
    SandboxEbayClient(live=True).create_listing(card, 57.99)
    assert ebay.uploads == 2  # front + back once, reused on the retry


def test_offer_reuse_updates_instead_of_creating(ebay, tmp_path):
    ebay.existing_offers = [{"offerId": "OFF-OLD", "status": "UNPUBLISHED"}]
    r = SandboxEbayClient(live=True).create_listing(make_card(tmp_path), 57.99)
    assert r.offer_id == "OFF-OLD"
    assert _bodies(ebay, "PUT", "/offer/OFF-OLD")
    assert not [c for c in ebay.calls if c[:2] == ("POST", "/sell/inventory/v1/offer")]


def test_publish_error_carries_ebays_message(ebay, tmp_path):
    ebay.publish_status = 400
    with pytest.raises(httpx.HTTPStatusError, match="Number of Cards is missing"):
        SandboxEbayClient(live=True).create_listing(make_card(tmp_path), 57.99)


def test_failed_lot_cleans_up_what_it_created(ebay, tmp_path):
    ebay.publish_status = 400
    cards = [make_card(tmp_path, id=1), make_card(tmp_path, id=2, condition="poor")]
    with pytest.raises(httpx.HTTPStatusError, match="Number of Cards"):
        SandboxEbayClient(live=True).create_set_listing(cards, 99.99)
    deleted = [p for m, p, _ in ebay.calls if m == "DELETE"]
    sku = listing_common.set_sku(cards)
    assert "/sell/inventory/v1/offer/OFF-NEW" in deleted
    assert f"/sell/inventory/v1/inventory_item/{sku}" in deleted
    inv = _bodies(ebay, "PUT", f"/inventory_item/{sku}")[0]
    assert inv["conditionDescriptors"] == [{"name": "40001", "values": ["400013"]}]


def test_failed_lot_never_deletes_an_already_published_offer(ebay, tmp_path):
    ebay.publish_status = 400
    ebay.existing_offers = [{"offerId": "OFF-LIVE", "status": "PUBLISHED"}]
    with pytest.raises(httpx.HTTPStatusError):
        SandboxEbayClient(live=True).create_set_listing([make_card(tmp_path, id=1)], 9.99)
    assert not [c for c in ebay.calls if c[0] == "DELETE"]


# --- photo fallback -------------------------------------------------------------

def test_media_failure_falls_back_to_checked_tunnel_url(ebay, tmp_path):
    ebay.upload_status = 500
    SandboxEbayClient(live=True).create_listing(make_card(tmp_path), 57.99)
    inv = _bodies(ebay, "PUT", "/inventory_item/CARD-7")[0]
    assert inv["product"]["imageUrls"] == [
        "https://tunnel.example.com/crops/7-front.jpg",
        "https://tunnel.example.com/crops/7-back.jpg",
    ]
    assert len([c for c in ebay.calls if c[0] == "HEAD"]) == 2


def test_tunnel_down_fails_with_a_clear_message(ebay, tmp_path):
    ebay.upload_status = 500
    ebay.head_status = 502
    with pytest.raises(media.ImageUnavailableError, match="tunnel"):
        SandboxEbayClient(live=True).create_listing(make_card(tmp_path), 57.99)
    assert not _bodies(ebay, "PUT", "/inventory_item/")  # nothing sent to eBay


def test_tunnel_serving_html_is_rejected(ebay, tmp_path):
    ebay.upload_status = 500
    ebay.head_type = "text/html"  # e.g. ngrok's offline page
    with pytest.raises(media.ImageUnavailableError, match="not an image"):
        SandboxEbayClient(live=True).create_listing(make_card(tmp_path), 57.99)


def test_media_uses_sandbox_host_in_sandbox(ebay, tmp_path):
    SandboxEbayClient(live=False).create_listing(make_card(tmp_path), 57.99)
    hosts = {c[1] for c in ebay.calls if "create_image_from_file" in c[1]}
    assert hosts == {"/commerce/media/v1_beta/image/create_image_from_file"}
    assert media._media_base(False).startswith("https://apim.sandbox.ebay.com")


# --- endpoints --------------------------------------------------------------------

@pytest.fixture
def api(ebay):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    def override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    db = Session()
    db.add(ImageUpload(id=1, filename="x.jpg"))
    db.commit()
    yield TestClient(app), db
    db.close()
    app.dependency_overrides.clear()


def _card(db, tmp_path, cid, **kw):
    base = dict(id=cid, upload_id=1, player="Ken Griffey Jr.", year="1989", set_brand="Upper Deck",
                card_number=str(cid), status="priced", estimated_price=50.0, price_basis="sold",
                crop_path=_photo(tmp_path, f"{cid}-f.jpg"))
    base.update(kw)
    card = Card(**base)
    db.add(card)
    db.commit()
    return card


def _published(db, cid, offer="OFF-1", sku=None, mode="live"):
    db.add(Listing(card_id=cid, ebay_mode=mode, sku=sku or f"CARD-{cid}", offer_id=offer,
                   listing_id="999", list_price=57.99, status="published"))
    db.commit()


def test_double_list_guard(api, tmp_path):
    client, db = api
    _card(db, tmp_path, 1)
    _published(db, 1)
    single = client.post("/api/cards/1/list")
    assert single.status_code == 409
    assert "already listed" in single.json()["message"]
    batch = client.post("/api/listings/sell", json={"card_ids": [1]}).json()["results"][0]
    assert batch["ok"] is False and "already listed" in batch["error"]
    assert db.query(Listing).filter_by(card_id=1).count() == 1  # no duplicate row


def test_bulk_results_carry_ok_error_and_url(api, tmp_path):
    client, db = api
    _card(db, tmp_path, 1)
    results = client.post("/api/listings/sell", json={"card_ids": [1, 404]}).json()["results"]
    good, missing = results
    assert good["ok"] is True and good["error"] is None
    assert good["listing_url"] == "https://www.ebay.com/itm/1234567890"
    assert good["list_price"] == 57.50
    assert missing == {**missing, "ok": False, "error": "not found", "listing_url": None}


def test_listing_below_floor_is_refused(api, tmp_path):
    client, db = api
    _card(db, tmp_path, 1)
    r = client.post("/api/listings/sell", json={"card_ids": [1], "prices": {"1": 1.0}}).json()
    assert r["results"][0]["ok"] is False and "floor" in r["results"][0]["error"]


def test_lot_with_a_listed_card_is_blocked(api, tmp_path):
    client, db = api
    _card(db, tmp_path, 1)
    _card(db, tmp_path, 2)
    _published(db, 2)
    r = client.post("/api/listings/sell-set", json={"card_ids": [1, 2]}).json()
    assert r["status"] == "blocked" and "card 2" in r["message"]


def test_failed_lot_is_recorded_per_card(api, ebay, tmp_path):
    client, db = api
    ebay.publish_status = 400
    _card(db, tmp_path, 1)
    _card(db, tmp_path, 2)
    r = client.post("/api/listings/sell-set", json={"card_ids": [1, 2]}).json()
    assert r["status"] == "failed" and "Number of Cards" in r["message"]
    rows = db.query(Listing).filter_by(status="failed").all()
    assert sorted(x.card_id for x in rows) == [1, 2]


def test_end_listing_marks_ended_for_whole_lot(api, ebay, tmp_path):
    client, db = api
    _card(db, tmp_path, 1)
    _card(db, tmp_path, 2)
    _published(db, 1, offer="LOT", sku="SET-1")
    _published(db, 2, offer="LOT", sku="SET-1")
    r = client.post("/api/listings/1/end")
    assert r.status_code == 200
    assert r.json()["card_ids"] == [1, 2] and r.json()["listing_state"] == "ended"
    assert ("POST", "/sell/inventory/v1/offer/LOT/withdraw", None) in ebay.calls
    assert client.post("/api/listings/1/end").status_code == 409
    # Ended cards can be listed again.
    assert client.post("/api/cards/1/list").json()["status"] == "published"


def test_end_listing_for_card_is_a_noop_without_a_live_listing(api, tmp_path):
    _, db = api
    card = _card(db, tmp_path, 1)
    assert orders.end_listing_for_card(db, card)["ended"] is False


def test_update_price(api, ebay, tmp_path):
    client, db = api
    _card(db, tmp_path, 1)
    _published(db, 1)
    ebay.offer_body = {
        "offerId": "OFF-1", "sku": "CARD-1", "status": "PUBLISHED", "availableQuantity": 1,
        "categoryId": "261328", "listingDescription": "<h2>x</h2>",
        "listingPolicies": {"fulfillmentPolicyId": "F", "paymentPolicyId": "P",
                            "returnPolicyId": "R",
                            "bestOfferTerms": {"bestOfferEnabled": True,
                                               "autoAcceptPrice": {"value": "46.39", "currency": "USD"}}},
        "merchantLocationKey": "LOC",
        "pricingSummary": {"price": {"value": "57.99", "currency": "USD"}},
        "listing": {"listingId": "999"},
    }
    r = client.post("/api/listings/1/price", json={"price": 40.0})
    assert r.status_code == 200 and r.json()["list_price"] == 40.0
    body = _bodies(ebay, "PUT", "/offer/OFF-1")[0]
    assert body["pricingSummary"]["price"]["value"] == "40.00"
    assert body["listingPolicies"]["bestOfferTerms"]["autoAcceptPrice"]["value"] == "32.00"
    assert "offerId" not in body and "listing" not in body and "status" not in body
    assert client.post("/api/listings/1/price", json={"price": 1.0}).status_code == 400
    assert client.post("/api/listings/404/price", json={"price": 9.0}).status_code == 404


def _envelope_offer(price, policy):
    return {
        "offerId": "OFF-1", "sku": "CARD-1", "status": "PUBLISHED", "availableQuantity": 1,
        "categoryId": "261328",
        "listingPolicies": {"fulfillmentPolicyId": policy, "paymentPolicyId": "P",
                            "returnPolicyId": "R"},
        "merchantLocationKey": "LOC",
        "pricingSummary": {"price": {"value": price, "currency": "USD"}},
    }


def test_price_above_envelope_limit_moves_card_to_parcel_policy(api, ebay, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "ebay_envelope_fulfillment_policy_id", "ENV")
    client, db = api
    _card(db, tmp_path, 1)
    _published(db, 1)
    ebay.offer_body = _envelope_offer("15.00", "ENV")
    ebay.item_body = {"product": {"title": "t"}, "condition": "USED_VERY_GOOD",
                      "packageWeightAndSize": {"packageType": "LETTER"}}
    assert client.post("/api/listings/1/price", json={"price": 25.0}).status_code == 200
    assert _bodies(ebay, "PUT", "/offer/OFF-1")[0]["listingPolicies"]["fulfillmentPolicyId"] == "F"
    item = _bodies(ebay, "PUT", "/inventory_item/CARD-1")[0]
    assert item["packageWeightAndSize"]["packageType"] == "PACKAGE_THICK_ENVELOPE"
    assert item["product"] == {"title": "t"}


def test_price_drop_under_limit_moves_card_to_envelope(api, ebay, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "ebay_envelope_fulfillment_policy_id", "ENV")
    client, db = api
    _card(db, tmp_path, 1)
    _published(db, 1)
    ebay.offer_body = _envelope_offer("25.00", "F")
    ebay.item_body = {"product": {"title": "t"}}
    assert client.post("/api/listings/1/price", json={"price": 12.0}).status_code == 200
    assert _bodies(ebay, "PUT", "/offer/OFF-1")[0]["listingPolicies"]["fulfillmentPolicyId"] == "ENV"
    assert _bodies(ebay, "PUT", "/inventory_item/CARD-1")[0]["packageWeightAndSize"]["packageType"] == "LETTER"


def test_price_change_within_envelope_range_leaves_package_alone(api, ebay, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "ebay_envelope_fulfillment_policy_id", "ENV")
    client, db = api
    _card(db, tmp_path, 1)
    _published(db, 1)
    ebay.offer_body = _envelope_offer("15.00", "ENV")
    assert client.post("/api/listings/1/price", json={"price": 10.0}).status_code == 200
    assert _bodies(ebay, "PUT", "/offer/OFF-1")[0]["listingPolicies"]["fulfillmentPolicyId"] == "ENV"
    assert not _bodies(ebay, "PUT", "/inventory_item/CARD-1")


def test_sync_sold_matches_by_sku_and_item_id(api, ebay, tmp_path):
    client, db = api
    for cid in (1, 2, 3):
        _card(db, tmp_path, cid)
    _published(db, 1, offer="O1")
    _published(db, 2, offer="O2", sku="CARD-2")
    _published(db, 3, offer="O3")
    db.query(Listing).filter_by(card_id=2).update({"listing_id": "222"})
    db.commit()
    ebay.orders = [
        {"orderId": "ORD-1", "creationDate": "2026-10-01T12:00:00.000Z",
         "lineItems": [{"sku": "CARD-1", "total": {"value": "55.00", "currency": "USD"}}]},
        {"orderId": "ORD-2", "creationDate": "2026-10-02T12:00:00.000Z",
         "lineItems": [{"legacyItemId": "222", "lineItemCost": {"value": "30.00"}}]},
        {"orderId": "ORD-3", "cancelStatus": {"cancelState": "CANCELED"},
         "lineItems": [{"sku": "CARD-3"}]},
    ]
    r = client.post("/api/listings/sync-sold").json()
    assert r["orders_checked"] == 3
    assert sorted(x["card_id"] for x in r["sold"]) == [1, 2]
    info = client.get("/api/listings/1").json()
    assert info["listing_state"] == "sold" and info["sold_price"] == 55.0
    assert client.get("/api/listings/3").json()["listing_state"] == "live"
    get_orders = [p for m, p, _ in ebay.calls if p == "/sell/fulfillment/v1/order"]
    assert get_orders
    # Sold cards cannot be listed again.
    assert client.post("/api/cards/1/list").status_code == 409


def test_get_listing_exposes_state_and_suggested_price(api, tmp_path):
    client, db = api
    _card(db, tmp_path, 1, price_basis="active", active_estimate=40.0, estimated_price=40.0)
    info = client.get("/api/listings/1").json()
    assert info["listing_state"] == "none"
    assert info["suggested_list_price"] == 38.00
    assert info["price_floor"] == 2.20
    assert client.get("/api/listings/404").status_code == 404


def test_listing_state_helper():
    from types import SimpleNamespace as NS

    assert orders.listing_state(NS(listings=[])) == "none"
    assert orders.listing_state(NS(listings=[NS(status="failed"), NS(status="preview")])) == "none"
    assert orders.listing_state(NS(listings=[NS(status="ended"), NS(status="published")])) == "live"
    assert orders.listing_state(NS(listings=[NS(status="ended")])) == "ended"
    assert orders.listing_state(NS(listings=[NS(status="sold"), NS(status="published")])) == "sold"


def test_sold_at_is_recorded(api, ebay, tmp_path):
    client, db = api
    _card(db, tmp_path, 1)
    _published(db, 1)
    ebay.orders = [{"orderId": "O", "creationDate": "2026-10-01T12:00:00.000Z",
                    "lineItems": [{"sku": "CARD-1", "total": {"value": "10"}}]}]
    client.post("/api/listings/sync-sold")
    row = db.query(Listing).filter_by(card_id=1).one()
    db.refresh(row)
    assert row.status == "sold" and row.order_id == "O"
    assert row.sold_at.replace(tzinfo=timezone.utc) == datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
