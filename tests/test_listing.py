"""Listing clients: preview never publishes; payload shape is correct."""
from types import SimpleNamespace

import pytest

from app.services.ebay.listing_common import (
    build_aspects,
    build_description,
    build_title,
    map_condition,
)
from app.services.ebay.preview import PreviewListingClient


def make_card(**kw):
    base = dict(id=7, year="1989", set_brand="Upper Deck", player="Ken Griffey Jr.",
                card_number="1", parallel=None, condition="near-mint", crop_path=None,
                sport="baseball")
    base.update(kw)
    return SimpleNamespace(**base)


def test_preview_never_publishes():
    r = PreviewListingClient().create_listing(make_card(), 75.0)
    assert r.status == "preview"
    assert r.listing_id is None  # nothing was listed
    assert r.list_price == 75.0
    assert r.response["preview"] is True
    # Captured payload is a real FIXED_PRICE (Buy-It-Now) offer for audit.
    assert r.response["payload"]["offer"]["format"] == "FIXED_PRICE"
    assert r.response["payload"]["offer"]["pricingSummary"]["price"]["value"] == "75.0"


def test_build_title_truncates_to_80():
    long = make_card(player="X" * 200)
    assert len(build_title(long)) <= 80


def test_aspects_omit_empty_values():
    card = make_card(parallel=None, card_number=None)
    aspects = build_aspects(card)
    assert "Parallel/Variety" not in aspects  # empty omitted
    assert "Card Number" not in aspects
    assert aspects["Player/Athlete"] == ["Ken Griffey Jr."]


def test_condition_mapping():
    assert map_condition(make_card(condition="near-mint"), "USED_VERY_GOOD") == "USED_VERY_GOOD"
    assert map_condition(make_card(condition="good"), "USED_VERY_GOOD") == "USED_ACCEPTABLE"
    assert map_condition(make_card(condition=None), "USED_VERY_GOOD") == "USED_VERY_GOOD"


def test_live_requires_credentials():
    from app.services.ebay.sandbox import MissingCredentialsError, SandboxEbayClient

    # No credentials configured in the test env -> clear error, no silent fake.
    with pytest.raises(MissingCredentialsError):
        SandboxEbayClient(live=True).create_listing(make_card(), 50.0)


# --- SET / lot listings ------------------------------------------------------

from app.services.ebay.listing_common import (  # noqa: E402
    MAX_IMAGES,
    build_set_aspects,
    build_set_description,
    build_set_title,
    set_image_urls,
    set_sku,
)


def _cards(n, **kw):
    return [make_card(id=i, **kw) for i in range(1, n + 1)]


def test_build_description_includes_identity_and_omits_blanks():
    desc = build_description(make_card(parallel=None, condition="near-mint"))
    assert "Ken Griffey Jr." in desc
    assert "1989" in desc and "Upper Deck" in desc
    assert "near-mint" in desc
    assert "Parallel" not in desc  # blank parallel is omitted
    assert desc.lstrip().startswith("<h2>")  # HTML allowed by eBay


def test_set_title_capped_at_80():
    cards = [make_card(id=i, player="X" * 50, set_brand="Y" * 50) for i in range(20)]
    assert len(build_set_title(cards)) <= 80


def test_set_title_leads_with_count():
    cards = _cards(12, sport="baseball")
    assert build_set_title(cards).startswith("12-Card Baseball Card Lot")


def test_set_aspects_has_number_of_cards_and_caps_values():
    cards = [make_card(id=i, player=f"Player {i}") for i in range(40)]
    aspects = build_set_aspects(cards)
    assert aspects["Number of Cards"] == ["40"]
    assert len(aspects["Player/Athlete"]) <= 30  # eBay max 30 values per aspect
    assert all(len(v) <= 65 for v in aspects["Player/Athlete"])


def test_set_description_lists_every_card_and_escapes_html():
    cards = _cards(3)
    cards[0].player = "<script>x</script>"
    desc = build_set_description(cards, shown_images=3)
    assert desc.count("<tr>") == 4  # header + 3 cards
    assert "<script>" not in desc  # escaped
    assert "&lt;script&gt;" in desc


def test_set_description_notes_image_overflow():
    cards = _cards(30)
    desc = build_set_description(cards, shown_images=MAX_IMAGES)
    assert f"{MAX_IMAGES} of 30" in desc


def test_set_image_urls_capped_at_24():
    # Crops are named <id>-<token>.jpg; the URL uses the actual filename.
    cards = [make_card(id=i, crop_path=f"/data/crops/{i}-tok.jpg") for i in range(1, 31)]
    urls = set_image_urls(cards, "https://imgs.example.com")
    assert len(urls) == MAX_IMAGES
    assert urls[0] == "https://imgs.example.com/crops/1-tok.jpg"


def test_set_sku_is_stable_short_and_unique():
    a = set_sku(_cards(3))
    b = set_sku(list(reversed(_cards(3))))  # order independent
    assert a == b and a.startswith("SET-1-3-") and len(a) <= 50
    assert set_sku(_cards(4)) != a


def test_preview_set_listing_never_publishes(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "public_image_base_url", "https://imgs.example.com")
    cards = _cards(5, crop_path="x.jpg")
    r = PreviewListingClient().create_set_listing(cards, 120.0)
    assert r.status == "preview" and r.listing_id is None
    offer = r.response["payload"]["offer"]
    assert offer["categoryId"] == "261329"  # lot category
    assert "<table" in offer["listingDescription"]
    assert r.response["payload"]["inventory_item"]["product"]["aspects"]["Number of Cards"] == ["5"]
    assert len(r.response["payload"]["inventory_item"]["product"]["imageUrls"]) == 5
