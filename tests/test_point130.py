"""130point results parsing: prices, dates, grades, best-offer tagging — no network."""
import httpx
import pytest

from app.services.point130 import (
    build_search_payload,
    parse_results_html,
    parse_sold_date,
)


def test_search_payload():
    payload = build_search_payload("Griffey 1989")
    assert payload["query"] == "Griffey 1989"
    assert payload["type"] == "2"


def test_parse_sold_date_variants():
    assert parse_sold_date("Sold Apr 12, 2026") == "2026-04-12"
    assert parse_sold_date("Dec 1 2025") == "2025-12-01"
    assert parse_sold_date("2026-04-12") == "2026-04-12"
    assert parse_sold_date("no date here") is None


def test_parse_results_extracts_fields():
    html = """
    <table>
    <tr class="sales">
      <td class="title">1989 Upper Deck Ken Griffey Jr. #1 PSA 10</td>
      <td><a href="/itm/123"><img src="https://i.ebayimg.com/a.jpg"/></a></td>
      <td>$120.50</td>
      <td>Apr 12, 2026</td>
    </tr>
    <tr class="sales">
      <th>Title</th><th>Price</th><th>Date</th>
    </tr>
    </table>"""
    comps = parse_results_html(html)
    assert len(comps) == 1  # header row (no price) skipped
    c = comps[0]
    assert c.sold_price == 120.50
    assert c.sold_date == "2026-04-12"
    assert c.condition_grade == "PSA 10"
    assert c.listing_url == "https://130point.com/itm/123"
    assert c.thumbnail_url == "https://i.ebayimg.com/a.jpg"
    assert c.kind == "sold"
    assert c.source == "130point (sold)"


def test_marketplace_detected_and_defaults_to_ebay():
    html = """
    <table>
      <tr class="sales"><td class="title">Card A</td><td>$10.00</td>
        <td>Jan 1, 2026</td><td>PWCC</td></tr>
      <tr class="sales"><td class="title">Card B</td><td>$20.00</td>
        <td>Jan 2, 2026</td><td>eBay</td></tr>
      <tr class="sales"><td class="title">Card C</td><td>$30.00</td>
        <td>Jan 3, 2026</td></tr>
    </table>"""
    comps = {c.title: c for c in parse_results_html(html)}
    assert comps["Card A"].marketplace == "PWCC"
    assert comps["Card B"].marketplace == "eBay"
    assert comps["Card C"].marketplace == "eBay"  # unlabeled rows default to eBay
    assert all(c.source.startswith("130point") for c in comps.values())


def test_best_offer_sale_is_tagged():
    html = """
    <table><tr class="sales">
      <td class="title">2018 Topps Shohei Ohtani RC</td>
      <td>$45.00</td>
      <td>Best Offer Accepted</td>
      <td>Mar 3, 2026</td>
    </tr></table>"""
    comps = parse_results_html(html)
    assert len(comps) == 1
    assert comps[0].sold_price == 45.00
    assert comps[0].source == "130point (sold, best offer)"


def test_duplicate_rows_deduped():
    row = (
        '<tr class="sales"><td class="title">Card A</td>'
        "<td>$10.00</td><td>Jan 1, 2026</td></tr>"
    )
    comps = parse_results_html(f"<table>{row}{row}</table>")
    assert len(comps) == 1


def test_no_price_returns_empty():
    html = '<table><tr class="sales"><td class="title">No price card</td></tr></table>'
    assert parse_results_html(html) == []


# --- Real markup shapes observed live on 130point (2026-08) -------------------
# Rows carry "Date: Thu 20 Aug 2026 03:34:35 GMT" (day-first, weekday prefix) and
# a structured tail "Sale Price: N - Best Offer Price: N - Sale Type: ...".

def test_parse_sold_date_day_first_with_weekday():
    """The live format. Without this every 130point comp is undated, which also
    means it bypasses the COMP_RECENCY_DAYS window entirely."""
    assert parse_sold_date("Date: Thu 20 Aug 2026 03:34:35 GMT") == "2026-08-20"
    assert parse_sold_date("Sun 3 Feb 2026 11:00:00 GMT") == "2026-02-03"


def test_month_first_dates_still_parse():
    assert parse_sold_date("Sold Apr 12, 2026") == "2026-04-12"
    assert parse_sold_date("Dec 1 2025") == "2025-12-01"


def _row(sale_price: str, best_offer: str, date_text: str = "Thu 20 Aug 2026 03:34:35 GMT") -> str:
    return f"""
    <table><tr class="sales">
      <td class="title">1989 Upper Deck Ken Griffey Jr. #1 PSA 10</td>
      <td><a href="/itm/1">link</a></td>
      <td>Sale Price: {sale_price} USD Date: {date_text}</td>
      <td>Sale Price: {sale_price} - Best Offer Price: {best_offer} - Bids: 0</td>
    </tr></table>"""


def test_zero_best_offer_price_is_not_a_best_offer_sale():
    """Every row contains the words 'Best Offer Price', so matching on the phrase
    alone tags 100% of sales as best offers."""
    comps = parse_results_html(_row("5,700.00", "0"))
    assert len(comps) == 1
    assert "best offer" not in (comps[0].source or "")


def test_a_real_best_offer_sale_is_still_tagged():
    comps = parse_results_html(_row("174.00", "150.00"))
    assert len(comps) == 1
    assert "best offer" in (comps[0].source or "")


def test_live_shaped_row_carries_its_date():
    comps = parse_results_html(_row("5,700.00", "0"))
    assert comps[0].sold_date == "2026-08-20"


# --- Cold-start empty response ------------------------------------------------
# Observed live: the first request after an idle period returns a ~114-byte body
# with no rows, and an identical query moments later returns the full results
# page. Without a retry, the first card priced after any pause silently loses
# this source.

def test_an_empty_first_response_is_retried_once(monkeypatch):
    from app.config import get_settings
    from app.services import point130

    monkeypatch.setattr(get_settings(), "point130_enabled", True)
    monkeypatch.setattr(point130.time, "sleep", lambda _s: None)
    bodies = ["", _row("120.00", "0")]
    calls = []

    def fake_post(url, **kw):
        calls.append(url)
        return httpx.Response(200, text=bodies[len(calls) - 1],
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(point130.httpx, "post", fake_post)
    comps = point130.fetch_sold_comps("1989 Upper Deck Ken Griffey Jr. #1")
    assert len(calls) == 2, "an empty page must be retried once"
    assert len(comps) == 1


def _serve(monkeypatch, status, body):
    from app.config import get_settings
    from app.services import point130

    monkeypatch.setattr(get_settings(), "point130_enabled", True)
    monkeypatch.setattr(point130.time, "sleep", lambda _s: None)
    calls = []

    def fake_post(url, **kw):
        calls.append(kw.get("data"))
        return httpx.Response(status, text=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(point130.httpx, "post", fake_post)
    return calls


def test_an_empty_stub_after_the_retry_is_reported_as_blocked(monkeypatch):
    """The 114-byte stub is the backend refusing, not "no sales". Reporting it
    as empty is how 130point silently contributed zero comps for months."""
    from app.services import point130

    calls = _serve(monkeypatch, 200, "")
    with pytest.raises(point130.Point130Error) as exc:
        point130.fetch_sold_comps("nonexistent card")
    assert exc.value.state == "blocked"
    assert len(calls) == 2, "exactly one retry, not an unbounded loop"


def test_a_full_page_with_no_rows_is_a_genuine_empty_result(monkeypatch):
    from app.services import point130

    page = "<html><body><table><tr><th>Title</th></tr></table>" + " " * 400 + "</body></html>"
    _serve(monkeypatch, 200, page)
    assert point130.fetch_sold_comps("nonexistent card") == []


CLOUDFLARE = """<!DOCTYPE html><html><head><title>Just a moment...</title></head>
<body><div id="challenge-platform">Checking your browser</div></body></html>"""


def test_cloudflare_challenge_is_reported_as_blocked(monkeypatch):
    from app.services import point130

    calls = _serve(monkeypatch, 200, CLOUDFLARE)
    with pytest.raises(point130.Point130Error) as exc:
        point130.fetch_sold_comps("1989 Upper Deck Ken Griffey Jr. #1")
    assert exc.value.state == "blocked"
    assert len(calls) == 1  # a challenge is not retried


@pytest.mark.parametrize("status", [403, 429, 503])
def test_refusal_statuses_are_reported_as_blocked(monkeypatch, status):
    from app.services import point130

    _serve(monkeypatch, status, "<html>nope</html>")
    with pytest.raises(point130.Point130Error) as exc:
        point130.fetch_sold_comps("griffey")
    assert exc.value.state == "blocked"


def test_network_failure_is_reported_as_an_error(monkeypatch):
    from app.config import get_settings
    from app.services import point130

    monkeypatch.setattr(get_settings(), "point130_enabled", True)

    def boom(url, **kw):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(point130.httpx, "post", boom)
    with pytest.raises(point130.Point130Error) as exc:
        point130.fetch_sold_comps("griffey")
    assert exc.value.state == "error"


def test_payload_carries_the_current_sales_page_fields():
    payload = build_search_payload("Griffey 1989")
    for field in ("query", "sort", "tab_id", "tz", "mp"):
        assert field in payload


# The row shape the current results page renders: one <tr id="dRow"> per sale
# with the price and currency as data attributes and the title/date in spans.
# The first link is the image link (no text), and the row also carries a list
# price and shipping that must NOT be read as the sale price.
DROW = """
<table>
 <tr id="dRow" data-price="88.00" data-currency="USD">
  <td id="dImg"><a href="https://www.ebay.com/itm/555"><img src="https://i.ebayimg.com/t.jpg"></a></td>
  <td id="dTitle">
   <span id="titleText"><a href="https://www.ebay.com/itm/555">2001 Topps Pedro Martinez #399</a></span>
   <span>List Price: 120.00 USD</span>
   <span id="dateText"><b>Date:</b> Thu 20 Aug 2026 03:34:35 GMT</span>
   <span>Sale Price: 88.00 - Best Offer Price: 88.00 - Shipping: 4.99</span>
  </td>
 </tr>
 <tr id="dRow" data-price="70.00" data-currency="GBP">
  <td id="dTitle"><span id="titleText"><a href="/x">2001 Topps Pedro Martinez #399</a></span>
  <span id="dateText">Date: Wed 19 Aug 2026 01:00:00 GMT</span></td>
 </tr>
</table>"""


def test_live_row_shape_reads_title_price_date_and_skips_non_usd():
    comps = parse_results_html(DROW)
    assert len(comps) == 1
    c = comps[0]
    assert c.title == "2001 Topps Pedro Martinez #399"
    assert c.sold_price == 88.00
    assert c.sold_date == "2026-08-20"
    assert c.listing_url == "https://www.ebay.com/itm/555"
    assert c.thumbnail_url == "https://i.ebayimg.com/t.jpg"
    assert c.source == "130point (sold, best offer)"


def test_sale_price_label_wins_over_list_price():
    html = """<table><tr class="sales"><td class="title">2001 Topps Pedro Martinez #399</td>
      <td>List Price: 120.00</td><td>Sale Price: 88.00 - Best Offer Price: 0</td>
      <td>Date: Thu 20 Aug 2026</td></tr></table>"""
    assert parse_results_html(html)[0].sold_price == 88.00
