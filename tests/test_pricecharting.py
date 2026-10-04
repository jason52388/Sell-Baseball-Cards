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


# --- pricing from a pasted SCP URL: identity parsing + URL guard ---

def test_is_scp_url():
    from app.services.pricecharting import is_scp_url
    assert is_scp_url("https://www.sportscardspro.com/game/baseball-cards-2001-topps/barry-bonds-497")
    assert is_scp_url("https://www.pricecharting.com/game/x/y")
    assert not is_scp_url("https://www.ebay.com/itm/123")
    assert not is_scp_url("not a url")


def test_ident_from_detail_parses_year_set_number_player():
    from app.services.pricecharting import ident_from_detail
    ident = ident_from_detail({
        "console-name": "Baseball Cards 2001 Topps",
        "product-name": "Barry Bonds #497",
    })
    assert ident == {
        "player": "Barry Bonds", "year": "2001",
        "set_brand": "Topps", "card_number": "497",
    }


def test_sales_premium_link_falls_back_to_product_page():
    # Premium-locked sale rows link to a generic upsell page; we replace that
    # with the card's own product page so the "view" link is useful.
    html = """<table class="hoverable-rows">
      <tr><td>2026-04-01</td>
          <td class="title"><a href="/sportscardspro-premium?f=salesPhotos">Bonds</a></td>
          <td>$2.00</td></tr></table>"""
    page = "https://www.sportscardspro.com/game/baseball-cards-2001-topps/barry-bonds-497"
    comps = parse_sales_table_html(html, page_url=page)
    assert comps and comps[0].listing_url == page


def test_ident_from_detail_strips_parallel_brackets():
    from app.services.pricecharting import ident_from_detail
    ident = ident_from_detail({
        "console-name": "Baseball Cards 1997 Upper Deck",
        "product-name": "Sammy Sosa [Global Impact] #189",
    })
    assert ident["player"] == "Sammy Sosa"
    assert ident["year"] == "1997"
    assert ident["set_brand"] == "Upper Deck"
    assert ident["card_number"] == "189"


# --- parallel/insert must be present, else no confident match (no base fallback) ---

PARALLEL_PRODUCTS = [
    {"id": "base", "product-name": "Sammy Sosa #331",
     "console-name": "Baseball Cards 1999 Upper Deck"},
    {"id": "insert", "product-name": "Sammy Sosa [Global Impact] #189",
     "console-name": "Baseball Cards 1999 Upper Deck"},
]


def test_requires_parallel_rejects_base_card():
    # Insert specified but only the base card exists -> no confident match
    # (must NOT silently price the base card).
    only_base = [PARALLEL_PRODUCTS[0]]
    assert (
        select_best_product(
            only_base, "1999 Upper Deck Sammy Sosa Global Impact",
            require_parallel="Global Impact",
        )
        is None
    )


def test_requires_parallel_picks_the_insert():
    best = select_best_product(
        PARALLEL_PRODUCTS, "1999 Upper Deck Sammy Sosa Global Impact",
        require_parallel="Global Impact",
    )
    assert best is not None and best["id"] == "insert"


def test_no_parallel_still_matches_base():
    # Base card (no parallel) keeps matching as before.
    best = select_best_product(
        [PARALLEL_PRODUCTS[0]], "1999 Upper Deck Sammy Sosa #331"
    )
    assert best is not None and best["id"] == "base"


# --- card number is authoritative: match on it even if parallel isn't in title ---

NUMBERED_PRODUCTS = [
    {"id": "189", "product-name": "Sammy Sosa #189",
     "console-name": "Baseball Cards 1997 Upper Deck"},
    {"id": "331", "product-name": "Sammy Sosa #331",
     "console-name": "Baseball Cards 1997 Upper Deck"},
]


def test_number_matches_even_when_parallel_absent():
    # A subset (e.g. "Global Impact") is catalogued as plain "#189". With the
    # number we must still match it, NOT reject it for lacking the parallel.
    best = select_best_product(
        NUMBERED_PRODUCTS, "1997 Upper Deck Sammy Sosa #189 Global Impact",
        require_parallel="Global Impact", require_number="189",
    )
    assert best is not None and best["id"] == "189"


def test_number_must_match_the_right_card():
    # Number present but wrong candidate number -> not selected.
    best = select_best_product(
        [NUMBERED_PRODUCTS[1]], "1997 Upper Deck Sammy Sosa #189",
        require_number="189",
    )
    assert best is None


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


# --- base cards must not resolve to a parallel product -------------------------

PEDRO = [
    {"id": "gold", "product-name": "Pedro Martinez #399",
     "console-name": "Baseball Cards 2001 Topps Gold"},
    {"id": "base", "product-name": "Pedro Martinez #399",
     "console-name": "Baseball Cards 2001 Topps"},
]


def test_base_card_prefers_base_product_over_gold_on_a_tie():
    best = select_best_product(PEDRO, "2001 Topps Pedro Martinez #399", require_number="399")
    assert best is not None and best["id"] == "base"
    best = select_best_product(list(reversed(PEDRO)), "2001 Topps Pedro Martinez #399",
                               require_number="399")
    assert best["id"] == "base"


def test_base_card_never_resolves_to_only_a_parallel_product():
    assert select_best_product([PEDRO[0]], "2001 Topps Pedro Martinez #399",
                               require_number="399") is None
    refractor = [{"id": "r", "product-name": "Pedro Martinez [Refractor] #399",
                  "console-name": "Baseball Cards 2001 Topps Chrome"}]
    assert select_best_product(refractor, "2001 Topps Chrome Pedro Martinez #399",
                               require_number="399") is None


def test_parallel_in_the_query_still_matches_its_product():
    best = select_best_product(PEDRO, "2001 Topps Gold Pedro Martinez #399 Gold",
                               require_parallel="Gold", require_number="399")
    assert best is not None and best["id"] == "gold"


# --- the player's name is required -------------------------------------------

def test_product_must_carry_the_players_last_name():
    hockey = [{"id": "h", "product-name": "Artemi Panarin #99",
               "console-name": "Hockey Cards 1993 Skybox"}]
    assert select_best_product(hockey, "1993 Skybox Barry Bonds #99",
                               require_number="99", require_player="Barry Bonds") is None
    bonds = [{"id": "b", "product-name": "Barry Bonds #99",
              "console-name": "Baseball Cards 1993 Skybox"}]
    assert select_best_product(bonds, "1993 Skybox Barry Bonds #99",
                               require_number="99", require_player="Barry Bonds")["id"] == "b"


def test_player_suffix_is_not_the_last_name():
    griffey = [{"id": "g", "product-name": "Ken Griffey Jr. #1",
                "console-name": "Baseball Cards 1989 Upper Deck"}]
    assert select_best_product(griffey, "1989 Upper Deck Ken Griffey Jr. #1",
                               require_number="1", require_player="Ken Griffey Jr.")


def test_lookup_passes_the_player_through(monkeypatch):
    from app.services import pricecharting as pc
    seen = {}

    def fake_select(products, query, **kw):
        seen.update(kw)
        return None

    monkeypatch.setattr(pc, "has_token", lambda: True)
    monkeypatch.setattr(pc, "select_best_product", fake_select)

    class R:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"products": []}

    monkeypatch.setattr(pc.httpx, "get", lambda *a, **k: R())
    pc.fetch_comps("1993 Skybox Barry Bonds", require_player="Barry Bonds")
    assert seen.get("require_player") == "Barry Bonds"


# --- scraped individual sales -------------------------------------------------

SCP_PAGE = """
<div id="completed-auctions-used">
 <table class="hoverable-rows"><tbody>
  <tr><td class="date">2026-09-01</td>
      <td class="title"><a href="https://www.ebay.com/itm/1">Pedro Martinez Topps card</a> [eBay]</td>
      <td class="numeric"><span class="js-price">$4.00</span></td></tr>
  <tr><td class="date">2026-09-02</td><td class="title"></td>
      <td class="numeric"><span class="js-price">$5.00</span></td></tr>
 </tbody></table>
</div>
<div id="completed-auctions-manual-only">
 <table class="hoverable-rows"><tbody>
  <tr><td class="date">2026-09-03</td>
      <td class="title"><a href="https://www.ebay.com/itm/3">Pedro Martinez 2001 Topps slab</a></td>
      <td class="numeric"><span class="js-price">$80.00</span></td></tr>
 </tbody></table>
</div>
<div id="completed-auctions-graded">
 <table class="hoverable-rows"><tbody>
  <tr><td class="date">2026-09-04</td>
      <td class="title"><a href="https://www.ebay.com/itm/4">Pedro Martinez 2001 Topps</a></td>
      <td class="numeric"><span class="js-price">$30.00</span></td></tr>
 </tbody></table>
</div>
"""
SCP_PRODUCT = "Baseball Cards 2001 Topps Pedro Martinez #399"


def test_scraped_sales_carry_the_product_title_and_grade_tier():
    from app.services.pricecharting import _dated_sales_from_html
    comps = _dated_sales_from_html(SCP_PAGE, "https://x/game/a/b", product_title=SCP_PRODUCT)
    by_price = {c.sold_price: c for c in comps}
    assert set(by_price) == {4.0, 5.0, 80.0, 30.0}
    raw = by_price[4.0]
    assert "Pedro Martinez Topps card" in raw.title
    assert "2001 Topps Pedro Martinez #399" in raw.title  # product identity attached
    assert "[eBay]" not in raw.title
    assert raw.condition_grade is None
    assert by_price[5.0].title == SCP_PRODUCT  # blank row title -> product title
    assert by_price[80.0].condition_grade == "PSA 10"
    assert by_price[30.0].condition_grade == "Graded"


def test_scraped_graded_rows_score_as_graded():
    from types import SimpleNamespace

    from app.services.matching import score_comp
    from app.services.pricecharting import _dated_sales_from_html
    card = SimpleNamespace(player="Pedro Martinez", year="2001", set_brand="Topps",
                           card_number="399", parallel=None)
    comps = _dated_sales_from_html(SCP_PAGE, "https://x/game/a/b", product_title=SCP_PRODUCT)
    types = {c.sold_price: score_comp(card, c).match_type for c in comps}
    assert types[4.0] == "exact"
    assert types[80.0] == "graded"
    assert types[30.0] == "graded"
