"""Incremental refresh: dated sold sales accumulate; active/undated replaced."""
from datetime import date, timedelta

from app.services.comp_cache import merge_comps
from app.services.ebay.base import SoldComp


def _sold(title, price, day, url=None, source="ebay"):
    return SoldComp(title=title, sold_price=price, sold_date=day,
                    listing_url=url, source=source, kind="sold")


def test_dated_sold_accumulate_across_refreshes():
    old = [_sold("A", 10.0, "2026-01-01", url="u1")]
    new = [_sold("B", 12.0, "2026-02-01", url="u2")]
    merged = merge_comps(old, new, retention_days=0)
    urls = {c.listing_url for c in merged}
    assert urls == {"u1", "u2"}  # history grows, old sale kept


def test_same_sale_deduped_by_url():
    old = [_sold("A", 10.0, "2026-01-01", url="u1")]
    new = [_sold("A (updated title)", 10.0, "2026-01-01", url="u1")]
    merged = merge_comps(old, new, retention_days=0)
    assert len(merged) == 1
    assert merged[0].title == "A (updated title)"  # newer copy wins


def test_active_comps_not_accumulated():
    old = [SoldComp(title="old ask", sold_price=20.0, source="ebay", kind="active")]
    new = [SoldComp(title="new ask", sold_price=22.0, source="ebay", kind="active")]
    merged = merge_comps(old, new, retention_days=0)
    titles = {c.title for c in merged}
    assert titles == {"new ask"}  # stale asking price dropped


def test_undated_sold_snapshot_replaced_not_piled_up():
    # e.g. SportsCardsPro aggregate market price (sold_date=None)
    old = [_sold("agg", 50.0, None, source="sportscardspro")]
    new = [_sold("agg", 55.0, None, source="sportscardspro")]
    merged = merge_comps(old, new, retention_days=0)
    assert len(merged) == 1 and merged[0].sold_price == 55.0


def test_retention_prunes_old_dated_sales():
    old_day = (date.today() - timedelta(days=400)).isoformat()
    fresh_day = (date.today() - timedelta(days=10)).isoformat()
    old = [_sold("stale", 9.0, old_day, url="u-old"),
           _sold("fresh", 11.0, fresh_day, url="u-fresh")]
    merged = merge_comps(old, [], retention_days=365)
    urls = {c.listing_url for c in merged}
    assert urls == {"u-fresh"}  # >365d sale pruned, recent kept


def test_undateable_sold_not_pruned():
    # A sold comp whose date couldn't be parsed must not be silently dropped.
    bad = SoldComp(title="x", sold_price=5.0, sold_date="recently",
                   source="ebay", kind="sold")
    # Unparseable date => treated as snapshot (kept from `new`), never pruned.
    merged = merge_comps([], [bad], retention_days=365)
    assert len(merged) == 1
