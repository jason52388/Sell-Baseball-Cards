"""Comp matching: exact/near/graded partitioning + exclusion."""
from types import SimpleNamespace

from app.services.ebay.base import SoldComp
from app.services.matching import partition, score_comp


def make_card(**kw):
    base = dict(player="Ken Griffey Jr.", year="1989", set_brand="Upper Deck",
                card_number="1", parallel=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_exact_match():
    card = make_card()
    comp = SoldComp(title="1989 Upper Deck Ken Griffey Jr. #1 RC", sold_price=120.0)
    assert score_comp(card, comp).match_type == "exact"


def test_excluded_wrong_player():
    card = make_card()
    comp = SoldComp(title="1989 Upper Deck Nolan Ryan #145", sold_price=20.0)
    assert score_comp(card, comp).match_type == "excluded"


def test_graded_tagged():
    card = make_card()
    comp = SoldComp(title="1989 Upper Deck Ken Griffey Jr. #1 PSA 10", sold_price=900.0)
    assert score_comp(card, comp).match_type == "graded"


def test_near_match_partial():
    card = make_card()
    comp = SoldComp(title="Ken Griffey Jr. baseball card lot", sold_price=15.0)
    assert score_comp(card, comp).match_type == "near"


def test_partition_counts():
    card = make_card()
    comps = [
        SoldComp(title="1989 Upper Deck Ken Griffey Jr. #1", sold_price=100.0),
        SoldComp(title="1989 Upper Deck Ken Griffey Jr. #1 PSA 10", sold_price=900.0),
        SoldComp(title="1989 Upper Deck Frank Thomas #2", sold_price=30.0),
    ]
    scored = partition(card, comps)
    types = [s.match_type for s in scored]
    assert types.count("exact") == 1
    assert types.count("graded") == 1
    assert types.count("excluded") == 1


# --- Graded detection ---------------------------------------------------------

def test_cgc_graded_not_counted_as_raw():
    """SportsCardsPro emits a CGC 10 tier on every raw lookup; if the matcher
    doesn't recognise CGC as a grade, that price lands in the RAW median."""
    card = make_card()
    comp = SoldComp(title="1989 Upper Deck Ken Griffey Jr. #1 [CGC 10]",
                    sold_price=900.0, condition_grade="CGC 10")
    assert score_comp(card, comp).match_type == "graded"


def test_csg_graded_not_counted_as_raw():
    card = make_card()
    comp = SoldComp(title="1989 Upper Deck Ken Griffey Jr. #1 CSG 9.5", sold_price=400.0)
    assert score_comp(card, comp).match_type == "graded"


# --- Exact-match criteria -----------------------------------------------------

def test_exact_match_without_year_when_set_and_number_match():
    """The rule is 'player + two of year/set/number'. A card with no year read
    should still price off comps matching its set and number."""
    card = make_card(year=None)
    comp = SoldComp(title="Upper Deck Ken Griffey Jr. #1 RC", sold_price=120.0)
    assert score_comp(card, comp).match_type == "exact"


# --- Token matching must respect word boundaries ------------------------------

def test_player_name_words_are_not_substring_matched():
    """'Bo' must not match inside 'Bob' — that prices one player off another
    player's sales."""
    card = make_card(player="Bo Jackson", year="1990", set_brand="Score",
                     card_number="697")
    comp = SoldComp(title="1990 Score Bob Jackson #697", sold_price=500.0)
    assert score_comp(card, comp).match_type == "excluded"


def test_player_still_matches_its_own_sale():
    card = make_card(player="Bo Jackson", year="1990", set_brand="Score",
                     card_number="697")
    comp = SoldComp(title="1990 Score Bo Jackson #697 RC", sold_price=40.0)
    assert score_comp(card, comp).match_type == "exact"


def test_year_is_not_substring_matched_inside_a_longer_number():
    card = make_card(player="Nolan Ryan", year="1989", set_brand="Topps",
                     card_number="530")
    comp = SoldComp(title="Nolan Ryan card lot 219890", sold_price=15.0)
    scored = score_comp(card, comp)
    assert "year" not in scored.match_reason


# --- Parallels and sets -------------------------------------------------------

def test_base_card_sale_is_not_an_exact_comp_for_a_parallel():
    """A /50 Gold parallel is worth many times the base card; base sales must
    not score exact for it."""
    card = make_card(player="Juan Soto", year="2023", set_brand="Topps",
                     card_number="100", parallel="Gold Foil")
    comp = SoldComp(title="2023 Topps Juan Soto #100 base", sold_price=2.0)
    assert score_comp(card, comp).match_type != "exact"


def test_parallel_card_matches_its_own_parallel_sale():
    card = make_card(player="Juan Soto", year="2023", set_brand="Topps",
                     card_number="100", parallel="Gold Foil")
    comp = SoldComp(title="2023 Topps Juan Soto #100 Gold Foil /50", sold_price=60.0)
    assert score_comp(card, comp).match_type == "exact"


def test_set_match_requires_all_significant_words():
    """'Topps Chrome' and plain 'Topps' are different products at different
    prices; one shared word must not count as a set match."""
    card = make_card(player="Juan Soto", year="2023", set_brand="Topps Chrome",
                     card_number="100")
    comp = SoldComp(title="2023 Topps Juan Soto #100", sold_price=2.0)
    assert "set" not in score_comp(card, comp).match_reason
