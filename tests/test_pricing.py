"""Pricing safeguards, status routing, recency + outlier trimming.

Pricing is fed a fake comp fetcher so tests never hit the network — and no
price is ever invented when there are no comps.
"""
from datetime import date, timedelta

from app.models import (
    STATUS_BELOW_THRESHOLD,
    STATUS_NEEDS_REVIEW,
    STATUS_PREVIEW,
    STATUS_PRICED,
    Card,
    ImageUpload,
)
from app.services.ebay.base import SoldComp
from app.services.pricing import finalize_card, preview_card, price_card


def fetcher(comps, graded_comps=None):
    def f(query, graded=False):
        return list(graded_comps or []) if graded else list(comps)
    return f


def persist_card(db, **kw):
    up = ImageUpload(filename="t.jpg")
    db.add(up)
    db.flush()
    defaults = dict(
        upload_id=up.id, player="Ken Griffey Jr.", year="1989",
        set_brand="Upper Deck", card_number="1", confidence=0.9,
    )
    defaults.update(kw)
    card = Card(**defaults)
    db.add(card)
    db.flush()
    return card


def exact_comps(price, n=3, sold_date=None):
    return [
        SoldComp(title="1989 Upper Deck Ken Griffey Jr. #1", sold_price=price,
                 sold_date=sold_date, source="ebay")
        for _ in range(n)
    ]


def test_low_confidence_needs_review(db_session):
    card = persist_card(db_session, confidence=0.4)
    price_card(card, db_session, fetcher(exact_comps(50)))
    assert card.status == STATUS_NEEDS_REVIEW
    assert "confidence" in card.review_reason


def test_incomplete_identity_needs_review(db_session):
    card = persist_card(db_session, player=None)
    price_card(card, db_session, fetcher(exact_comps(50)))
    assert card.status == STATUS_NEEDS_REVIEW
    assert "incomplete" in card.review_reason


def test_no_comps_needs_review_no_invented_price(db_session):
    card = persist_card(db_session)
    price_card(card, db_session, fetcher([]))
    assert card.status == STATUS_NEEDS_REVIEW
    assert card.estimated_price is None  # never invents a number


def test_priced_above_threshold(db_session):
    card = persist_card(db_session)
    price_card(card, db_session, fetcher(exact_comps(50)))
    assert card.status == STATUS_PRICED
    assert card.estimated_price == 50.0


def test_below_threshold(db_session):
    card = persist_card(db_session)
    price_card(card, db_session, fetcher(exact_comps(2.0)))
    assert card.status == STATUS_BELOW_THRESHOLD


def test_outlier_is_trimmed(db_session):
    # A tight $40-$60 cluster plus one absurd $5000 mismatch -> $5000 is trimmed
    # and the median stays ~$50.
    card = persist_card(db_session)
    prices = [40, 50, 50, 50, 50, 50, 60, 5000]
    comps = [c for p in prices for c in exact_comps(p, n=1)]
    price_card(card, db_session, fetcher(comps))
    assert card.estimated_price == 50.0       # robust to the $5000 outlier
    assert card.sold_estimate == 50.0
    assert "7 recent SOLD" in card.derivation  # 1 of 8 trimmed


def test_stale_comps_excluded_by_recency(db_session):
    card = persist_card(db_session)
    old = (date.today() - timedelta(days=400)).isoformat()
    price_card(card, db_session, fetcher(exact_comps(50, n=3, sold_date=old)))
    # All comps are too old -> no usable price -> needs_review.
    assert card.estimated_price is None
    assert card.status == STATUS_NEEDS_REVIEW


def test_recent_comps_kept(db_session):
    card = persist_card(db_session)
    recent = (date.today() - timedelta(days=10)).isoformat()
    price_card(card, db_session, fetcher(exact_comps(50, n=3, sold_date=recent)))
    assert card.estimated_price == 50.0


def test_psa10_override_kept_for_review(db_session):
    card = persist_card(db_session, psa10_candidate=True)
    price_card(card, db_session, fetcher(exact_comps(2.0), graded_comps=exact_comps(800.0)))
    assert card.status == STATUS_NEEDS_REVIEW
    assert "PSA 10" in card.review_reason
    assert card.graded_value_estimate == 800.0


def test_anomaly_override(db_session):
    card = persist_card(db_session, anomaly_flag=True)
    price_card(card, db_session, fetcher(exact_comps(50)))
    assert card.status == STATUS_NEEDS_REVIEW
    assert "anomaly" in card.review_reason


def test_low_comp_count_note(db_session):
    card = persist_card(db_session)
    price_card(card, db_session, fetcher(exact_comps(50, n=1)))
    assert card.status == STATUS_PRICED
    assert "low-confidence" in card.review_reason


def active_comps(price, n=3):
    return [
        SoldComp(title="1989 Upper Deck Ken Griffey Jr. #1", sold_price=price,
                 source="ebay (active)", kind="active")
        for _ in range(n)
    ]


def test_active_only_fallback(db_session):
    card = persist_card(db_session)
    price_card(card, db_session, fetcher(active_comps(60)))
    assert card.estimated_price == 60.0
    assert card.price_basis == "active"
    assert card.active_estimate == 60.0
    assert card.sold_estimate is None
    assert "ASKING" in card.derivation


def src_comps(price, source, n=1, kind="sold"):
    return [
        SoldComp(title="1989 Upper Deck Ken Griffey Jr. #1", sold_price=price,
                 source=source, kind=kind)
        for _ in range(n)
    ]


def test_pricecharting_is_primary_sold_source(db_session):
    card = persist_card(db_session)
    # PriceCharting $52 should drive the estimate over 3 eBay sold at $40.
    comps = src_comps(52, "sportscardspro") + src_comps(40, "ebay (sold)", n=3)
    price_card(card, db_session, fetcher(comps))
    assert card.sold_estimate == 52.0
    assert card.price_basis == "sold"
    assert "sportscardspro" in card.derivation


def test_falls_back_when_primary_absent(db_session):
    card = persist_card(db_session)
    comps = src_comps(40, "ebay (sold)", n=3)  # no PriceCharting
    price_card(card, db_session, fetcher(comps))
    assert card.sold_estimate == 40.0
    assert "ebay" in card.derivation


def test_sold_preferred_over_active(db_session):
    card = persist_card(db_session)
    comps = exact_comps(50) + active_comps(80)
    price_card(card, db_session, fetcher(comps))
    assert card.price_basis == "sold"
    assert card.estimated_price == 50.0      # sold drives the estimate
    assert card.sold_estimate == 50.0
    assert card.active_estimate == 80.0      # but both are recorded


def test_records_source_and_reference_image(db_session):
    card = persist_card(db_session)
    comps = [SoldComp(title="1989 Upper Deck Ken Griffey Jr. #1", sold_price=50.0,
                      thumbnail_url="https://i.ebayimg.com/x.jpg", source="ebay")]
    price_card(card, db_session, fetcher(comps))
    assert card.price_sources == "ebay"
    assert card.reference_image_url == "https://i.ebayimg.com/x.jpg"


def test_preview_low_confidence_still_gets_reference_photo(db_session):
    """A low-confidence card stays in preview but still fetches a reference photo
    + tentative estimate so the user can verify the match before adding."""
    card = persist_card(db_session, confidence=0.3)
    comps = [SoldComp(title="1989 Upper Deck Ken Griffey Jr. #1", sold_price=50.0,
                      thumbnail_url="https://i.ebayimg.com/x.jpg", source="ebay")]
    preview_card(card, db_session, fetcher(comps))
    assert card.status == STATUS_PREVIEW
    assert card.reference_image_url == "https://i.ebayimg.com/x.jpg"
    assert card.estimated_price == 50.0


def test_finalize_routes_preview_by_confidence(db_session):
    from app.config import get_settings

    high = persist_card(db_session, confidence=0.9)
    preview_card(high, db_session, fetcher(exact_comps(50)))
    finalize_card(high, get_settings())
    assert high.status == STATUS_PRICED

    low = persist_card(db_session, confidence=0.3)
    preview_card(low, db_session, fetcher(exact_comps(50)))
    finalize_card(low, get_settings())
    assert low.status == STATUS_NEEDS_REVIEW
    assert "confidence" in low.review_reason
