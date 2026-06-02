"""gather_comps aggregation + honest notes (no network)."""
from app.services import comp_sources, pricecharting
from app.services.ebay import browse, browser_scrape, insights
from app.services.ebay.base import SoldComp


def _silence_all(monkeypatch):
    monkeypatch.setattr(insights, "is_enabled", lambda: False)
    monkeypatch.setattr(pricecharting, "has_token", lambda: False)
    monkeypatch.setattr(browser_scrape, "is_enabled", lambda: False)
    monkeypatch.setattr(comp_sources, "scrape_sold", lambda q: [])
    # No eBay creds via settings is the default in tests.


def test_no_sources_configured_note(monkeypatch):
    _silence_all(monkeypatch)
    comps, notes = comp_sources.gather_comps("griffey")
    assert comps == []
    assert any("No price source configured" in n for n in notes)


def test_pricecharting_contributes_sold(monkeypatch):
    _silence_all(monkeypatch)
    monkeypatch.setattr(pricecharting, "has_token", lambda: True)
    monkeypatch.setattr(
        pricecharting, "fetch_comps",
        lambda q, graded=False: [SoldComp(title=q, sold_price=52.0,
                                          source="sportscardspro", kind="sold")],
    )
    comps, notes = comp_sources.gather_comps("1989 Upper Deck Ken Griffey Jr #1")
    assert len(comps) == 1
    assert comps[0].source == "sportscardspro"
    assert comps[0].kind == "sold"


def test_browser_scrape_contributes_when_enabled(monkeypatch):
    _silence_all(monkeypatch)
    monkeypatch.setattr(browser_scrape, "is_enabled", lambda: True)
    monkeypatch.setattr(
        browser_scrape, "fetch_sold_comps",
        lambda q, graded=False: [SoldComp(title=q, sold_price=49.0,
                                          source="ebay (sold, scraped)", kind="sold")],
    )
    comps, _ = comp_sources.gather_comps("griffey")
    assert comps and comps[0].source == "ebay (sold, scraped)"
