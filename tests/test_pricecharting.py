"""PriceCharting JSON parsing (pennies -> dollars, ungraded vs PSA 10)."""
from app.services.pricecharting import parse_pricecharting_json, select_best_product

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
