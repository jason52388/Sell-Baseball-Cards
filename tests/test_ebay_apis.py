"""Browse (active) + Marketplace Insights (sold) JSON parsing — no network."""
from app.services.ebay.browse import parse_browse_json
from app.services.ebay.insights import parse_insights_json


def test_parse_browse_active():
    data = {
        "itemSummaries": [
            {
                "title": "1989 Upper Deck Ken Griffey Jr. #1",
                "price": {"value": "74.99", "currency": "USD"},
                "condition": "Ungraded",
                "itemWebUrl": "https://www.ebay.com/itm/111",
                "image": {"imageUrl": "https://i.ebayimg.com/a.jpg"},
            }
        ]
    }
    comps = parse_browse_json(data)
    assert len(comps) == 1
    c = comps[0]
    assert c.kind == "active"          # asking price, not sold
    assert c.sold_price == 74.99
    assert c.sold_date is None
    assert c.source == "ebay (active)"
    assert c.listing_url == "https://www.ebay.com/itm/111"


def test_parse_insights_sold():
    data = {
        "itemSales": [
            {
                "title": "1989 Upper Deck Ken Griffey Jr. #1 PSA 9",
                "lastSoldPrice": {"value": "52.00", "currency": "USD"},
                "lastSoldDate": "2026-05-18T12:00:00.000Z",
                "itemWebUrl": "https://www.ebay.com/itm/222",
                "image": {"imageUrl": "https://i.ebayimg.com/b.jpg"},
            }
        ]
    }
    comps = parse_insights_json(data)
    assert len(comps) == 1
    c = comps[0]
    assert c.kind == "sold"            # a real completed sale
    assert c.sold_price == 52.00
    assert c.sold_date == "2026-05-18"  # date-only
    assert c.condition_grade == "PSA 9"
    assert c.source == "ebay (sold)"
