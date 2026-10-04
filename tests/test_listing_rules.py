"""Listing rules: condition, title, item specifics, images, price, payload."""
from types import SimpleNamespace

import pytest

from app.services.ebay import listing_common as lc


def make_card(**kw):
    base = dict(
        id=7, year="1989", set_brand="Upper Deck", player="Ken Griffey Jr.",
        card_number="1", parallel=None, serial_number=None, condition="near-mint",
        crop_path=None, back_crop_path=None, reference_image_url=None,
        sport="baseball", estimated_price=10.0, price_basis="sold",
        active_estimate=None, sold_estimate=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def settings(**kw):
    base = dict(
        price_markup=1.15, ebay_ask_undercut=0.95, ebay_fee_pct=0.1325,
        ebay_per_order_fee=0.40, ebay_shipping_supplies_cost=1.00, ebay_min_net=0.50,
        ebay_best_offer_auto_accept_pct=0.80, ebay_marketplace_id="EBAY_US",
        ebay_category_id="261328", ebay_lot_category_id="261329",
        ebay_fulfillment_policy_id="F", ebay_payment_policy_id="P",
        ebay_return_policy_id="R", ebay_merchant_location_key="LOC",
        ebay_condition="USED_VERY_GOOD", public_image_base_url="https://img.example.com",
        ebay_include_reference_image=False,
        ebay_package_weight_oz=3.0, ebay_lot_extra_card_weight_oz=0.25,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# --- 1. condition -------------------------------------------------------------

@pytest.mark.parametrize("cond", ["mint", "near-mint", "excellent", "very good", "good", "poor", None, "weird"])
def test_raw_cards_are_always_ungraded_used_very_good(cond):
    assert lc.map_condition(make_card(condition=cond)) == "USED_VERY_GOOD"
    descs = lc.build_condition_descriptors(make_card(condition=cond))
    assert [d["name"] for d in descs] == ["40001"]


def test_grade_estimate_is_not_a_slab():
    card = make_card(condition="near-mint", grade_estimate="PSA 9")
    assert lc.map_condition(card) == "USED_VERY_GOOD"


def test_slabbed_card_uses_graded_condition_and_descriptors():
    card = make_card(condition="PSA 9")
    assert lc.map_condition(card) == "LIKE_NEW"
    descs = {d["name"]: d["values"] for d in lc.build_condition_descriptors(card)}
    assert descs == {"27501": ["275010"], "27502": ["275022"]}
    bgs = {d["name"]: d["values"] for d in lc.build_condition_descriptors(make_card(condition="BGS 9.5"))}
    assert bgs == {"27501": ["275013"], "27502": ["275021"]}
    sgc10 = {d["name"]: d["values"] for d in lc.build_condition_descriptors(make_card(condition="SGC 10 slab"))}
    assert sgc10 == {"27501": ["275016"], "27502": ["275020"]}


def test_lot_uses_worst_condition():
    cards = [make_card(id=1, condition="near-mint"), make_card(id=2, condition="poor"),
             make_card(id=3, condition="excellent")]
    assert lc.worst_condition_card(cards).id == 2


# --- 8. item specifics --------------------------------------------------------

@pytest.mark.parametrize("brand,maker", [
    ("Topps Chrome", "Topps"), ("Bowman Draft", "Topps"), ("Upper Deck", "Upper Deck"),
    ("Fleer Ultra", "Fleer"), ("Donruss", "Panini"), ("Panini Prizm", "Panini"),
    ("Score", "Score"), ("Leaf", "Leaf"), ("Mystery Brand", None), (None, None),
])
def test_manufacturer_from_set(brand, maker):
    assert lc.manufacturer(brand) == maker


def test_aspects_are_rich_and_capped():
    card = make_card(
        player="Mike Trout", year="2011", set_brand="Topps Update", card_number="US175",
        serial_number="23/99", team="Los Angeles Angels", rookie=True, subset="Rookie Debut",
        parallel="Gold " + "X" * 100,
    )
    a = lc.build_aspects(card)
    assert a["Manufacturer"] == ["Topps"]
    assert a["Team"] == ["Los Angeles Angels"]
    assert a["League"] == ["Major League (MLB)"]
    assert a["Graded"] == ["No"]
    assert a["Autographed"] == ["No"]
    assert set(a["Features"]) >= {"Rookie", "Serial Numbered"}
    assert a["Print Run"] == ["99"]
    assert a["Card Name"] == ["Mike Trout"]
    assert a["Vintage"] == ["No"]
    assert a["Original/Licensed Reprint"] == ["Original"]
    assert a["Type"] == ["Sports Trading Card"]
    assert a["Season"] == ["2011"] and a["Year Manufactured"] == ["2011"]
    assert a["Insert Set"] == ["Rookie Debut"]
    assert all(len(v) <= 65 for vals in a.values() for v in vals)


def test_aspects_vintage_and_works_without_new_card_fields():
    a = lc.build_aspects(make_card(year="1975"))
    assert a["Vintage"] == ["Yes"]
    assert "Team" not in a and "Features" not in a


def test_graded_aspects():
    a = lc.build_aspects(make_card(condition="PSA 8"))
    assert a["Graded"] == ["Yes"]
    assert a["Professional Grader"] == ["Professional Sports Authenticator (PSA)"]
    assert a["Grade"] == ["8"]


# --- 9. titles ----------------------------------------------------------------

def test_title_priority_order():
    card = make_card(player="Mike Trout", year="2011", set_brand="Topps Update",
                     card_number="US175", rookie=True, team="Angels", serial_number="5/99",
                     parallel="Gold")
    t = lc.build_title(card)
    assert t.startswith("2011 Topps Update Mike Trout Gold #US175 /99 RC")
    assert len(t) <= 80


def test_title_trims_whole_words_only():
    card = make_card(player="Ken Griffey Jr.", set_brand="Upper Deck Collector's Choice Special Edition",
                     subset="Star Rookies Extraordinary Insert", parallel="Gold Signature Refractor",
                     card_number="1", team="Seattle Mariners")
    t = lc.build_title(card)
    assert len(t) <= 80
    words = set(" ".join([card.player, card.set_brand, card.subset, card.parallel,
                          card.team, "1989 #1 Baseball Card"]).split())
    assert all(w in words for w in t.split())


def test_title_never_cuts_mid_word_when_one_field_is_huge():
    t = lc.build_title(make_card(player="X" * 200))
    assert len(t) <= 80
    assert "XXXX" not in t  # an 200-char word is dropped, not chopped


# --- 2/3. images --------------------------------------------------------------

def test_image_paths_include_back_and_never_reference():
    card = make_card(crop_path="/d/crops/7-a.jpg", back_crop_path="/d/crops/7-b.jpg",
                     reference_image_url="/refimg/x.jpg")
    assert lc.card_image_urls(card, "https://img.example.com") == [
        "https://img.example.com/crops/7-a.jpg", "https://img.example.com/crops/7-b.jpg",
    ]
    with_ref = lc.card_image_urls(card, "https://img.example.com", include_reference=True)
    assert with_ref[-1] == "https://img.example.com/refimg/x.jpg"
    assert lc.listing_image_paths(card) == ["/d/crops/7-a.jpg", "/d/crops/7-b.jpg"]


# --- 11. price rule -----------------------------------------------------------

def test_price_floor():
    # (0.40 + 1.00 + 0.50) / (1 - 0.1325) = 2.1902, rounded UP to the cent
    assert lc.listing_price_floor(settings()) == 2.20


def test_sold_basis_uses_markup_and_rounds_up_to_half_dollar():
    assert lc.suggested_list_price(make_card(estimated_price=50.0, price_basis="sold"), settings()) == 57.50
    # 1.5 markup: 7.10 x 1.5 = 10.65 -> 11.00; 7.00 x 1.5 = 10.50 stays
    s = settings(price_markup=1.5)
    assert lc.suggested_list_price(make_card(estimated_price=7.10, price_basis="sold"), s) == 11.00
    assert lc.suggested_list_price(make_card(estimated_price=7.00, price_basis="sold"), s) == 10.50
    assert lc.suggested_list_price(make_card(estimated_price=6.90, price_basis="sold"), s) == 10.50


def test_round_up_half():
    assert lc.round_up_half(12.01) == 12.50
    assert lc.round_up_half(12.50) == 12.50
    assert lc.round_up_half(12.51) == 13.00
    assert lc.round_up_half(0.10) == 0.50
    assert lc.round_up_half(1.0, floor=2.20) == 2.50


def test_asking_basis_undercuts_median_ask():
    card = make_card(estimated_price=40.0, price_basis="active", active_estimate=40.0)
    assert lc.suggested_list_price(card, settings()) == 38.00


def test_cheap_card_never_below_floor():
    p = lc.suggested_list_price(make_card(estimated_price=0.5), settings())
    assert p >= lc.listing_price_floor(settings())
    assert p == 2.50


def test_no_estimate_returns_none():
    assert lc.suggested_list_price(make_card(estimated_price=None), settings()) is None


def test_best_offer_terms():
    t = lc.best_offer_terms(56.99, settings())
    assert t["bestOfferEnabled"] is True
    assert t["autoAcceptPrice"]["value"] == "45.59"
    assert t["autoDeclinePrice"]["value"] == "2.20"
    # At the floor, Best Offer cannot accept anything lower, so it is off.
    assert lc.best_offer_terms(2.20, settings()) is None


# --- 10. one payload builder --------------------------------------------------

def test_single_payload_shape():
    card = make_card()
    p = lc.build_single_payload(card, 56.99, settings(), ["https://i.ebayimg.com/a.jpg"])
    assert p["sku"] == "CARD-7"
    inv, offer = p["inventory_item"], p["offer"]
    assert inv["product"]["imageUrls"] == ["https://i.ebayimg.com/a.jpg"]
    assert inv["condition"] == "USED_VERY_GOOD"
    assert offer["availableQuantity"] == 1
    assert offer["merchantLocationKey"] == "LOC"
    assert offer["listingPolicies"]["bestOfferTerms"]["bestOfferEnabled"] is True
    assert "bestOfferTerms" not in offer
    assert offer["listingDescription"].startswith("<h2>")
    assert offer["pricingSummary"]["price"] == {"value": "56.99", "currency": "USD"}


def test_lot_payload_uses_worst_condition_and_lot_category():
    cards = [make_card(id=1, condition="near-mint"), make_card(id=2, condition="poor")]
    p = lc.build_lot_payload(cards, 20.0, settings(), [])
    assert p["offer"]["categoryId"] == "261329"
    assert p["inventory_item"]["conditionDescriptors"] == [{"name": "40001", "values": ["400013"]}]


# --- 11. package weight and size (calculated shipping needs them) -------------

def test_single_payload_carries_package_weight_and_size():
    p = lc.build_single_payload(make_card(), 10.0, settings(), [])
    pkg = p["inventory_item"]["packageWeightAndSize"]
    assert pkg["weight"] == {"value": 3.0, "unit": "OUNCE"}
    assert pkg["dimensions"] == {"length": 7.0, "width": 4.0, "height": 1.0, "unit": "INCH"}
    assert pkg["packageType"] == "PACKAGE_THICK_ENVELOPE"


def test_lot_package_weight_grows_with_each_extra_card():
    cards = [make_card(id=i) for i in range(1, 5)]
    p = lc.build_lot_payload(cards, 20.0, settings(), [])
    assert p["inventory_item"]["packageWeightAndSize"]["weight"] == {"value": 3.75, "unit": "OUNCE"}
