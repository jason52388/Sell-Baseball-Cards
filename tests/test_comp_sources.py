"""gather_comps aggregation + honest notes (no network)."""
from app.services import comp_sources, point130, pricecharting
from app.services.ebay import browse, browser_scrape, insights
from app.services.ebay.base import SoldComp


def _silence_all(monkeypatch):
    monkeypatch.setattr(insights, "is_enabled", lambda: False)
    monkeypatch.setattr(pricecharting, "has_token", lambda: False)
    monkeypatch.setattr(pricecharting, "fetch_grade_tiers", lambda q, **kw: [])
    monkeypatch.setattr(pricecharting, "fetch_individual_sales", lambda q, **kw: [])
    monkeypatch.setattr(point130, "is_enabled", lambda: False)
    monkeypatch.setattr(browser_scrape, "is_enabled", lambda: False)
    monkeypatch.setattr(comp_sources, "scrape_sold", lambda q: [])
    # Silence eBay Browse explicitly so the suite stays hermetic even when real
    # EBAY_CLIENT_ID/SECRET are present in .env (otherwise it hits the live API).
    monkeypatch.setattr(browse, "has_credentials", lambda: False)
    s = comp_sources.get_settings()
    monkeypatch.setattr(s, "ebay_client_id", "")
    monkeypatch.setattr(s, "ebay_client_secret", "")
    monkeypatch.setattr(s, "sportscardspro_sales_enabled", False)
    comp_sources.reset_health()


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
        lambda q, graded=False, **kw: [SoldComp(title=q, sold_price=52.0,
                                                source="sportscardspro", kind="sold")],
    )
    comps, notes = comp_sources.gather_comps("1989 Upper Deck Ken Griffey Jr #1", use_cache=False)
    assert len(comps) == 1
    assert comps[0].source == "sportscardspro"
    assert comps[0].kind == "sold"


def test_pricecharting_adds_grade_tiers_and_sales(monkeypatch):
    _silence_all(monkeypatch)
    monkeypatch.setattr(comp_sources.get_settings(), "sportscardspro_sales_enabled", True)
    monkeypatch.setattr(pricecharting, "has_token", lambda: True)
    monkeypatch.setattr(
        pricecharting, "fetch_comps",
        lambda q, graded=False, **kw: [SoldComp(title=q, sold_price=52.0,
                                                condition_grade="Ungraded",
                                                source="sportscardspro", kind="sold")],
    )
    monkeypatch.setattr(
        pricecharting, "fetch_grade_tiers",
        lambda q, **kw: [SoldComp(title=f"{q} [PSA 10]", sold_price=380.0,
                                  condition_grade="PSA 10", source="sportscardspro", kind="sold")],
    )
    monkeypatch.setattr(
        pricecharting, "fetch_individual_sales",
        lambda q, **kw: [SoldComp(title=q, sold_price=49.0, sold_date="2026-04-01",
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


# --- Per-source status, honest notes, health ---------------------------------------

import httpx  # noqa: E402

from app.services import comp_cache  # noqa: E402


def _sold(q, price=50.0, source="ebay (sold)"):
    return [SoldComp(title=q, sold_price=price, sold_date="2026-09-01",
                     source=source, kind="sold")]


def _with_insights(monkeypatch, fetch):
    monkeypatch.setattr(insights, "is_enabled", lambda: True)
    monkeypatch.setattr(insights, "fetch_sold_comps", fetch)


def test_insights_switched_off_adds_no_note(monkeypatch):
    _silence_all(monkeypatch)
    s = comp_sources.get_settings()
    monkeypatch.setattr(s, "ebay_client_id", "id")
    monkeypatch.setattr(s, "ebay_client_secret", "secret")
    monkeypatch.setattr(browse, "fetch_active_comps", lambda q, graded=False: [])
    _, notes = comp_sources.gather_comps("griffey", use_cache=False)
    assert not any("Insights" in n for n in notes)


def test_insights_server_error_is_reported_not_raised(monkeypatch):
    """A 500 from Insights used to escape and fail pricing for the whole card."""
    _silence_all(monkeypatch)
    from app.services.ebay import insights as ins

    class Resp:
        status_code = 500

    monkeypatch.setattr(ins, "is_enabled", lambda: True)
    monkeypatch.setattr(ins, "get_app_access_token", lambda **kw: "tok")
    monkeypatch.setattr(ins.httpx, "get", lambda *a, **k: Resp())
    monkeypatch.setattr(point130, "is_enabled", lambda: True)
    monkeypatch.setattr(point130, "fetch_sold_comps", lambda q, graded=False: _sold(q, 45.0))

    result = comp_sources.collect_comps("griffey", use_cache=False)
    assert [c.sold_price for c in result.comps] == [45.0]  # other sources still count
    by = {st.source: st for st in result.statuses}
    assert by["insights"].state == "error" and "500" in by["insights"].message
    assert by["130point"].state == "ok"
    assert result.notes[0].startswith(comp_sources.SOURCE_PROBLEM_PREFIX)


def test_insights_rate_limit_is_quota(monkeypatch):
    _silence_all(monkeypatch)
    from app.services.ebay import insights as ins

    class Resp:
        status_code = 429

    monkeypatch.setattr(ins, "is_enabled", lambda: True)
    monkeypatch.setattr(ins, "get_app_access_token", lambda **kw: "tok")
    monkeypatch.setattr(ins.httpx, "get", lambda *a, **k: Resp())
    result = comp_sources.collect_comps("griffey", use_cache=False)
    assert result.statuses[0].state == "quota"


def test_expired_token_is_reported_and_never_cached(monkeypatch):
    _silence_all(monkeypatch)
    monkeypatch.setattr(pricecharting, "has_token", lambda: True)

    def expired(q, **kw):
        raise pricecharting.PriceChartingAuthError("SportsCardsPro rejected the API token: expired")

    monkeypatch.setattr(pricecharting, "fetch_comps", expired)
    monkeypatch.setattr(point130, "is_enabled", lambda: True)
    monkeypatch.setattr(point130, "fetch_sold_comps", lambda q, graded=False: _sold(q))
    monkeypatch.setattr(comp_cache, "get", lambda *a, **k: None)
    puts = []
    monkeypatch.setattr(comp_cache, "put", lambda *a, **k: puts.append(k))

    result = comp_sources.collect_comps("griffey")
    assert result.statuses[0].state == "auth_expired"
    assert puts == [], "a result with a failed source must not be cached"
    assert "rejected the API token" in result.notes[0]


def test_a_clean_result_is_cached_through_the_callers_session(monkeypatch):
    _silence_all(monkeypatch)
    monkeypatch.setattr(point130, "is_enabled", lambda: True)
    monkeypatch.setattr(point130, "fetch_sold_comps", lambda q, graded=False: _sold(q))
    monkeypatch.setattr(comp_cache, "get", lambda *a, **k: None)
    puts = []
    monkeypatch.setattr(comp_cache, "put", lambda *a, **k: puts.append(k))
    sentinel = object()
    comp_sources.gather_comps("griffey", db=sentinel)
    assert puts and puts[0]["db"] is sentinel


def test_empty_result_is_not_cached(monkeypatch):
    _silence_all(monkeypatch)
    monkeypatch.setattr(comp_cache, "get", lambda *a, **k: None)
    puts = []
    monkeypatch.setattr(comp_cache, "put", lambda *a, **k: puts.append(k))
    comp_sources.gather_comps("griffey")
    assert puts == []


def test_130point_blocked_is_a_blocked_status(monkeypatch):
    _silence_all(monkeypatch)
    monkeypatch.setattr(point130, "is_enabled", lambda: True)

    def blocked(q, graded=False):
        raise point130.Point130Error("130point served a bot-check page", state="blocked")

    monkeypatch.setattr(point130, "fetch_sold_comps", blocked)
    result = comp_sources.collect_comps("griffey", use_cache=False)
    assert result.statuses[0].state == "blocked"


def test_browse_server_error_is_reported(monkeypatch):
    _silence_all(monkeypatch)
    s = comp_sources.get_settings()
    monkeypatch.setattr(s, "ebay_client_id", "id")
    monkeypatch.setattr(s, "ebay_client_secret", "secret")
    monkeypatch.setattr(browse, "has_credentials", lambda: True)
    monkeypatch.setattr(browse, "get_app_access_token", lambda **kw: "tok")
    monkeypatch.setattr(browse, "_result_cache", {})

    def fake_get(url, **kw):
        return httpx.Response(500, request=httpx.Request("GET", url))

    monkeypatch.setattr(browse.httpx, "get", fake_get)
    result = comp_sources.collect_comps("griffey", use_cache=False)
    st = next(x for x in result.statuses if x.source == "ebay_browse")
    assert st.state == "error"


def test_health_tracks_last_error_and_last_success(monkeypatch):
    _silence_all(monkeypatch)
    monkeypatch.setattr(pricecharting, "has_token", lambda: True)

    def expired(q, **kw):
        raise pricecharting.PriceChartingAuthError("SportsCardsPro rejected the API token: expired")

    monkeypatch.setattr(pricecharting, "fetch_comps", expired)
    comp_sources.collect_comps("griffey", use_cache=False)
    health = comp_sources.source_health()
    scp = next(e for e in health["sources"] if e["source"] == "sportscardspro")
    assert scp["state"] == "auth_expired" and scp["ok"] is False
    assert "expired" in scp["last_error"]
    assert "SportsCardsPro" in health["banner"]

    monkeypatch.setattr(pricecharting, "fetch_comps", lambda q, **kw: _sold(q, source="sportscardspro"))
    comp_sources.collect_comps("griffey", use_cache=False)
    scp = next(e for e in comp_sources.source_health()["sources"] if e["source"] == "sportscardspro")
    assert scp["ok"] is True and scp["last_success_at"]
    assert "expired" in scp["last_error"]  # the last error stays visible
    assert comp_sources.source_health()["banner"] is None


def test_health_survives_a_restart_through_the_db(monkeypatch, db_session):
    _silence_all(monkeypatch)
    monkeypatch.setattr(point130, "is_enabled", lambda: True)

    def blocked(q, graded=False):
        raise point130.Point130Error("130point refused the request (HTTP 403)", state="blocked")

    monkeypatch.setattr(point130, "fetch_sold_comps", blocked)
    comp_sources.collect_comps("griffey", use_cache=False)
    comp_sources.persist_health(db_session)
    comp_sources.reset_health()  # simulated restart
    health = comp_sources.source_health(db_session)
    assert health["problems"][0]["source"] == "130point"
    assert "403" in health["banner"]
