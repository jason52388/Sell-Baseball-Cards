"""gather_comps aggregation + honest notes (no network)."""
from app.services import comp_sources, point130, pricecharting
from app.services.ebay import browse, browser_scrape, insights
from app.services.ebay.base import SoldComp


def _silence_all(monkeypatch):
    monkeypatch.setattr(insights, "is_enabled", lambda: False)
    monkeypatch.setattr(pricecharting, "has_token", lambda: False)
    monkeypatch.setattr(pricecharting, "fetch_grade_tiers", lambda q: [])
    monkeypatch.setattr(pricecharting, "fetch_individual_sales", lambda q: [])
    monkeypatch.setattr(point130, "is_enabled", lambda: False)
    monkeypatch.setattr(browser_scrape, "is_enabled", lambda: False)
    monkeypatch.setattr(comp_sources, "scrape_sold", lambda q: [])
    # Silence eBay Browse explicitly so the suite stays hermetic even when real
    # EBAY_CLIENT_ID/SECRET are present in .env (otherwise it hits the live API).
    monkeypatch.setattr(browse, "has_credentials", lambda: False)
    s = comp_sources.get_settings()
    monkeypatch.setattr(s, "ebay_client_id", "")
    monkeypatch.setattr(s, "ebay_client_secret", "")


def test_no_sources_configured_note(monkeypatch):
    _silence_all(monkeypatch)
    comps, notes = comp_sources.gather_comps("griffey", use_cache=False)
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
    comps, notes = comp_sources.gather_comps("1989 Upper Deck Ken Griffey Jr #1", use_cache=False)
    assert len(comps) == 1
    assert comps[0].source == "sportscardspro"
    assert comps[0].kind == "sold"


def test_pricecharting_adds_grade_tiers_and_sales(monkeypatch):
    _silence_all(monkeypatch)
    monkeypatch.setattr(pricecharting, "has_token", lambda: True)
    monkeypatch.setattr(
        pricecharting, "fetch_comps",
        lambda q, graded=False: [SoldComp(title=q, sold_price=52.0,
                                          condition_grade="Ungraded",
                                          source="sportscardspro", kind="sold")],
    )
    monkeypatch.setattr(
        pricecharting, "fetch_grade_tiers",
        lambda q: [SoldComp(title=f"{q} [PSA 10]", sold_price=380.0,
                            condition_grade="PSA 10", source="sportscardspro", kind="sold")],
    )
    monkeypatch.setattr(
        pricecharting, "fetch_individual_sales",
        lambda q: [SoldComp(title=q, sold_price=49.0, sold_date="2026-04-01",
                            source="sportscardspro (sold)", kind="sold")],
    )
    comps, _ = comp_sources.gather_comps("griffey", use_cache=False)
    sources = {c.source for c in comps}
    assert sources == {"sportscardspro", "sportscardspro (sold)"}
    assert any(c.condition_grade == "PSA 10" for c in comps)  # tier breakdown present
    assert any(c.sold_date == "2026-04-01" for c in comps)    # individual dated sale


def test_point130_contributes_when_enabled(monkeypatch):
    _silence_all(monkeypatch)
    monkeypatch.setattr(point130, "is_enabled", lambda: True)
    monkeypatch.setattr(
        point130, "fetch_sold_comps",
        lambda q, graded=False: [SoldComp(title=q, sold_price=45.0,
                                          source="130point (sold, best offer)", kind="sold")],
    )
    comps, _ = comp_sources.gather_comps("ohtani rc", use_cache=False)
    assert comps and comps[0].source == "130point (sold, best offer)"


def test_browser_scrape_contributes_when_enabled(monkeypatch):
    _silence_all(monkeypatch)
    monkeypatch.setattr(browser_scrape, "is_enabled", lambda: True)
    monkeypatch.setattr(
        browser_scrape, "fetch_sold_comps",
        lambda q, graded=False: [SoldComp(title=q, sold_price=49.0,
                                          source="ebay (sold, scraped)", kind="sold")],
    )
    comps, _ = comp_sources.gather_comps("griffey", use_cache=False)
    assert comps and comps[0].source == "ebay (sold, scraped)"
