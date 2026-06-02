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
