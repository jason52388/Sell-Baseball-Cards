"""eBay sold-results parsing: prices, dates, grades, links — no network."""
from app.services.ebay.scrape import (
    build_sold_search_url,
    parse_sold_date,
    parse_sold_html,
)


def test_sold_search_url():
    url = build_sold_search_url("Griffey 1989")
    assert "LH_Sold=1" in url and "LH_Complete=1" in url


def test_parse_sold_date():
    assert parse_sold_date("Sold  Apr 12, 2026") == "2026-04-12"
    assert parse_sold_date("Sold Dec 1, 2025") == "2025-12-01"
    assert parse_sold_date("no date here") is None


def test_parse_sold_html_extracts_fields():
    html = """
    <ul>
    <li class="s-item">
      <div class="s-item__title">1989 Upper Deck Ken Griffey Jr. #1 PSA 10</div>
      <span class="s-item__price">$120.50</span>
      <a class="s-item__link" href="https://www.ebay.com/itm/123"></a>
      <div class="s-item__image-wrapper"><img src="https://i.ebayimg.com/a.jpg"/></div>
      <div class="s-item__caption">Sold  Apr 12, 2026</div>
    </li>
    <li class="s-item">
      <div class="s-item__title">Shop on eBay</div>
      <span class="s-item__price">$0.99</span>
    </li>
    </ul>"""
    comps = parse_sold_html(html)
    assert len(comps) == 1  # "Shop on eBay" template filtered out
    c = comps[0]
    assert c.sold_price == 120.50
    assert c.sold_date == "2026-04-12"
    assert c.condition_grade == "PSA 10"
    assert c.listing_url == "https://www.ebay.com/itm/123"
    assert c.thumbnail_url == "https://i.ebayimg.com/a.jpg"


def test_price_range_takes_first():
    html = """<li class="s-item"><div class="s-item__title">card</div>
      <span class="s-item__price">$10.00 to $25.00</span></li>"""
    assert parse_sold_html(html)[0].sold_price == 10.00
