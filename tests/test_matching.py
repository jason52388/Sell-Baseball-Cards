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
    comp = SoldComp(title="Ken Griffey Jr. baseball card", sold_price=15.0)
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


# --- Grade detection: words between the grader and the number ------------------

import pytest

from app.services.matching import GRADE_RE, detect_grade


@pytest.mark.parametrize("title,grade", [
    ("1989 Upper Deck Ken Griffey Jr. #1 PSA Gem Mint 10", "PSA 10"),
    ("1989 Upper Deck Ken Griffey Jr. #1 PSA GEM MT 10", "PSA 10"),
    ("1989 Upper Deck Ken Griffey Jr. #1 BGS Pristine 10", "BGS 10"),
    ("1989 Upper Deck Ken Griffey Jr. #1 SGC 9.5 Mint+", "SGC 9.5"),
    ("1989 Upper Deck Ken Griffey Jr. #1 PSA NM-MT 8", "PSA 8"),
    ("1989 Upper Deck Ken Griffey Jr. #1 PSA10", "PSA 10"),
    ("1989 Upper Deck Ken Griffey Jr. #1 BGS 9.5 Gem Mint", "BGS 9.5"),
    ("1989 Upper Deck Ken Griffey Jr. #1 CGC Pristine 10", "CGC 10"),
    ("1989 Upper Deck Ken Griffey Jr. #1 PSA-9", "PSA 9"),
])
def test_grade_with_words_between_grader_and_number(title, grade):
    assert detect_grade(title) == grade
    card = make_card()
    assert score_comp(card, SoldComp(title=title, sold_price=500.0)).match_type == "graded"


@pytest.mark.parametrize("title", [
    "2001 Topps Pedro Martinez #399 Auto PSA/DNA Certified",
    "2001 Topps Pedro Martinez #399 PSA DNA authenticated 2003",
    "2001 Topps Pedro Martinez #399 near mint",
])
def test_autograph_authentication_alone_is_not_a_grade(title):
    assert GRADE_RE.search(title) is None


def test_browse_condition_graded_counts_as_graded():
    """eBay Browse reports condition 'Graded' for slabs whose title may not
    name the grade; those sell for slab money and must not price a raw card."""
    card = make_card()
    comp = SoldComp(title="1989 Upper Deck Ken Griffey Jr. #1", sold_price=300.0,
                    condition_grade="Graded", kind="active")
    assert score_comp(card, comp).match_type == "graded"


def test_shared_grade_regex_is_used_by_every_source():
    from app.services import point130, pricecharting
    from app.services.ebay import scrape
    assert pricecharting._GRADE_RE is GRADE_RE
    assert point130._GRADE_RE is GRADE_RE
    assert scrape._detect_grade("Griffey CGC 10") == "CGC 10"


# --- Parallels the card does NOT have ------------------------------------------

def pedro(**kw):
    return make_card(player="Pedro Martinez", year="2001", set_brand="Topps",
                     card_number="399", **kw)


@pytest.mark.parametrize("title", [
    "2001 Topps Gold Pedro Martinez #399",
    "2001 Topps Pedro Martinez #399 Refractor",
    "2001 Topps Pedro Martinez #399 /2001",
    "2001 Topps Pedro Martinez #399 Printing Plate 1/1",
    "2001 Topps Pedro Martinez #399 SP Variation",
    "2001 Topps Pedro Martinez #399 Chrome",
    "2001 Topps Pedro Martinez #399 Holo Foil",
    "2001 Topps Pedro Martinez #399 Black parallel",
])
def test_base_card_excludes_parallel_sales(title):
    scored = score_comp(pedro(), SoldComp(title=title, sold_price=40.0))
    assert scored.match_type == "excluded"
    assert "parallel" in scored.match_reason


def test_gold_inside_a_name_is_not_a_parallel():
    card = make_card(player="Paul Goldschmidt", year="2013", set_brand="Topps",
                     card_number="12")
    comp = SoldComp(title="2013 Topps Paul Goldschmidt #12", sold_price=3.0)
    assert score_comp(card, comp).match_type == "exact"


def test_marker_inside_the_cards_own_set_name_is_allowed():
    card = make_card(player="Derek Jeter", year="1999", set_brand="Topps Gold Label",
                     card_number="5")
    comp = SoldComp(title="1999 Topps Gold Label Derek Jeter #5", sold_price=8.0)
    assert score_comp(card, comp).match_type == "exact"
    chrome = make_card(player="Derek Jeter", year="1999", set_brand="Topps Chrome",
                       card_number="5")
    comp = SoldComp(title="1999 Topps Chrome Derek Jeter #5", sold_price=8.0)
    assert score_comp(chrome, comp).match_type == "exact"


def test_parallel_card_rejects_a_different_parallel():
    card = pedro(parallel="Refractor")
    comp = SoldComp(title="2001 Topps Pedro Martinez #399 Gold Refractor /50", sold_price=90.0)
    assert score_comp(card, comp).match_type == "excluded"


def test_subset_is_a_bonus_not_a_requirement():
    card = make_card(subset="League Leaders")
    plain = SoldComp(title="1989 Upper Deck Ken Griffey Jr. #1", sold_price=10.0)
    assert score_comp(card, plain).match_type == "exact"
    named = SoldComp(title="1989 Upper Deck Ken Griffey Jr. #1 League Leaders", sold_price=10.0)
    scored = score_comp(card, named)
    assert scored.match_type == "exact" and "subset" in scored.match_reason


# --- Junk listings ---------------------------------------------------------------

@pytest.mark.parametrize("title", [
    "Ken Griffey Jr. 1989 Upper Deck #1 lot of 5",
    "1989 Upper Deck Ken Griffey Jr. #1 x10",
    "1989 Upper Deck Ken Griffey Jr. #1 (10) bundle",
    "1989 Upper Deck Ken Griffey Jr. #1 you pick",
    "1989 Upper Deck Ken Griffey Jr. #1 Reprint",
    "1989 Upper Deck Ken Griffey Jr. #1 RP",
    "1989 Upper Deck Ken Griffey Jr. #1 custom ACEO",
    "1989 Upper Deck Ken Griffey Jr. #1 art card",
    "1989 Upper Deck Ken Griffey Jr. #1 digital NFT",
    "Topps Bunt 1989 Upper Deck Ken Griffey Jr. #1",
    "1989 Upper Deck Ken Griffey Jr. #1 case break",
    "1989 Upper Deck Ken Griffey Jr. #1 facsimile replica",
])
def test_junk_listings_are_excluded_with_a_reason(title):
    scored = score_comp(make_card(), SoldComp(title=title, sold_price=5.0))
    assert scored.match_type == "excluded"
    assert scored.match_reason.startswith("junk listing")


def test_junk_words_are_whole_word_only():
    card = make_card(player="Ken Griffey Jr.")
    comp = SoldComp(title="1989 Upper Deck Ken Griffey Jr. #1 Breakthrough Lotus", sold_price=5.0)
    assert score_comp(card, comp).match_type == "exact"
