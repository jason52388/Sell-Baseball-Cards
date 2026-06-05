"""PriceCharting JSON parsing (pennies -> dollars, ungraded vs PSA 10)."""
from app.services.pricecharting import (
    parse_grade_tiers,
    parse_pricecharting_json,
    parse_sales_table_html,
    parse_sold_date,
    product_page_url,
    select_best_product,
)

SAMPLE = {
    "status": "success",
    "product-name": "Ken Griffey Jr. #1",
    "console-name": "1989 Upper Deck",
    "loose-price": 5200,        # $52.00 ungraded
    "manual-only-price": 38000,  # $380.00 PSA 10
}


def test_ungraded_price():
    comps = parse_pricecharting_json(SAMPLE)
    assert len(comps) == 1
    c = comps[0]
    assert c.sold_price == 52.00
    assert c.condition_grade == "Ungraded"
    assert c.kind == "sold"
    assert c.source == "sportscardspro"
    assert "1989 Upper Deck Ken Griffey Jr. #1" == c.title


def test_graded_price():
    comps = parse_pricecharting_json(SAMPLE, graded=True)
    assert comps[0].sold_price == 380.00
    assert comps[0].condition_grade == "PSA 10"


def test_missing_price_returns_empty():
    assert parse_pricecharting_json({"product-name": "x", "console-name": "y"}) == []


def test_error_status_returns_empty():
    assert parse_pricecharting_json({"status": "error"}) == []


# --- product selection: reject wrong products, pick the real card ---

PRODUCTS = [
    {"id": "1", "product-name": "Michael Jordan 3 Times In A Row #222",
     "console-name": "Funko POP Basketball"},
    {"id": "2", "product-name": "Michael Jordan #57",
     "console-name": "1986 Fleer"},
]


def test_selects_correct_card_not_funko():
    best = select_best_product(PRODUCTS, "1986 Fleer Michael Jordan #57")
    assert best is not None
    assert best["id"] == "2"  # the real card, not the Funko POP


def test_rejects_when_year_absent():
    # Query year 1986 not present in any candidate -> no confident match.
    only_funko = [PRODUCTS[0]]
    assert select_best_product(only_funko, "1986 Fleer Michael Jordan #57") is None


def test_rejects_unrelated():
    assert select_best_product(PRODUCTS, "1990 Topps Nolan Ryan #4") is None


# --- grade-tier breakdown (aggregate, informational) ---

TIERS_SAMPLE = {
    "status": "success",
    "product-name": "Ken Griffey Jr. #1",
    "console-name": "1989 Upper Deck",
    "loose-price": 5200,
    "graded-price": 12000,       # PSA 9
    "manual-only-price": 38000,  # PSA 10
    "bgs-10-price": 95000,       # BGS 10
}


def test_grade_tiers_excludes_ungraded_and_labels_grades():
    comps = parse_grade_tiers(TIERS_SAMPLE)
    grades = {c.condition_grade for c in comps}
    assert grades == {"PSA 9", "PSA 10", "BGS 10"}  # ungraded excluded
    assert all(c.source == "sportscardspro" and c.kind == "sold" for c in comps)
    assert any(c.sold_price == 950.0 and "[BGS 10]" in c.title for c in comps)


def test_grade_tiers_skips_missing_fields():
    comps = parse_grade_tiers({"status": "success", "product-name": "X",
                               "console-name": "Y", "graded-price": 1000})
    assert [c.condition_grade for c in comps] == ["PSA 9"]


def test_product_page_url_slugifies():
    url = product_page_url(SAMPLE)
    assert url.endswith("/game/1989-upper-deck/ken-griffey-jr-1")


# --- individual sales table scrape ---

def test_parse_sales_table_extracts_dated_sales():
    html = """
    <table>
      <tr><th>Date</th><th>Title</th><th>Price</th></tr>
      <tr><td>2026-04-01</td><td class="title"><a href="/itm/9">Griffey PSA 10</a></td>
          <td>$365.00</td></tr>
      <tr><td>2026-03-15</td><td class="title">Griffey raw</td><td>$48.00</td></tr>
    </table>"""
    comps = parse_sales_table_html(html, page_url="https://www.sportscardspro.com/game/x/y")
    assert len(comps) == 2  # header row (no price) skipped
    first = comps[0]
    assert first.sold_price == 365.00
    assert first.sold_date == "2026-04-01"
    assert first.condition_grade == "PSA 10"
    assert first.listing_url == "https://www.sportscardspro.com/itm/9"
    assert first.source == "sportscardspro (sold)"
    assert first.marketplace == "eBay"  # SCP's recent-sales are eBay completed sales
    assert first.kind == "sold"


def test_parse_sales_table_dedupes_and_skips_priceless_rows():
    html = """<table>
      <tr><td>x</td></tr>
      <tr><td>2026-01-01</td><td class="title">Card</td><td>$10.00</td></tr>
      <tr><td>2026-01-01</td><td class="title">Card</td><td>$10.00</td></tr>
    </table>"""
    assert len(parse_sales_table_html(html)) == 1


def test_sales_date_parsing_variants():
    assert parse_sold_date("Apr 1, 2026") == "2026-04-01"
    assert parse_sold_date("2026-04-01") == "2026-04-01"
    assert parse_sold_date("nope") is None
