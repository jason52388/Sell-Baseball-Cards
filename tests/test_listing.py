"""Listing clients: preview never publishes; payload shape is correct."""
from types import SimpleNamespace

import pytest

from app.services.ebay.listing_common import build_aspects, build_title, map_condition
from app.services.ebay.preview import PreviewListingClient


def make_card(**kw):
    base = dict(id=7, year="1989", set_brand="Upper Deck", player="Ken Griffey Jr.",
                card_number="1", parallel=None, condition="near-mint", crop_path=None)
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
