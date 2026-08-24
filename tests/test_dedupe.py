"""Finding duplicate cards in the library.

The rule: compare only fields BOTH cards have a value for; any disagreement on a
compared field means they are not duplicates. All five present and equal is
"certain"; agreeing on what was read with something missing is "possible".
"""
from types import SimpleNamespace

from app.services.dedupe import find_duplicates


def card(cid, player="Barry Bonds", year="2001", set_brand="Topps",
         card_number="497", parallel=None, side="front", status="priced"):
    return SimpleNamespace(
        id=cid, player=player, year=year, set_brand=set_brand,
        card_number=card_number, parallel=parallel, side=side, status=status,
    )


def ids(group):
    return sorted(c.id for c in group.cards)


# --- Certain ------------------------------------------------------------------

def test_two_identical_cards_are_a_certain_duplicate():
    groups = find_duplicates([card(1), card(2)])
    assert len(groups) == 1
    assert groups[0].tier == "certain"
    assert ids(groups[0]) == [1, 2]


def test_three_copies_form_one_group_of_three():
    groups = find_duplicates([card(1), card(2), card(3)])
    assert len(groups) == 1
    assert ids(groups[0]) == [1, 2, 3]


def test_base_cards_with_no_parallel_still_count_as_certain():
    """Parallel is legitimately absent on a base card; both absent is agreement,
    not a missing field."""
    groups = find_duplicates([card(1, parallel=None), card(2, parallel=None)])
    assert groups and groups[0].tier == "certain"


def test_matching_parallels_are_certain():
    groups = find_duplicates([card(1, parallel="Gold Foil"), card(2, parallel="Gold Foil")])
    assert groups and groups[0].tier == "certain"


def test_the_group_is_labelled_with_the_card_identity():
    groups = find_duplicates([card(1), card(2)])
    label = groups[0].label
    assert "Barry Bonds" in label and "2001" in label and "497" in label


# --- Not duplicates -----------------------------------------------------------

def test_same_player_year_and_set_but_different_numbers_are_not_duplicates():
    """The real Kerry Wood case: 2001 Topps #786 and #623."""
    a = card(1, player="Kerry Wood", card_number="786", parallel="Golden Moments")
    b = card(2, player="Kerry Wood", card_number="623")
    assert find_duplicates([a, b]) == []


def test_different_parallel_is_not_a_duplicate():
    a = card(1, parallel="Gold Foil")
    b = card(2, parallel="Refractor")
    assert find_duplicates([a, b]) == []


def test_different_years_are_not_duplicates():
    assert find_duplicates([card(1, year="2001"), card(2, year="2000")]) == []


def test_a_lone_card_is_not_a_duplicate():
    assert find_duplicates([card(1)]) == []


def test_cards_with_no_player_are_never_grouped():
    """An unreadable scan must not pool with every other unreadable scan."""
    assert find_duplicates([card(1, player=None), card(2, player=None)]) == []


# --- Possible -----------------------------------------------------------------

def test_a_missing_card_number_makes_it_possible_not_certain():
    groups = find_duplicates([card(1, card_number="497"), card(2, card_number=None)])
    assert len(groups) == 1
    assert groups[0].tier == "possible"
    assert ids(groups[0]) == [1, 2]
    assert "number" in groups[0].reason.lower()


def test_an_ambiguous_wildcard_is_not_guessed_into_a_group():
    """One card with no number, two with different numbers: we cannot know which
    it belongs to, so we say nothing rather than guess."""
    cards = [card(1, card_number="497"), card(2, card_number="250"),
             card(3, card_number=None)]
    assert find_duplicates(cards) == []


def test_possible_needs_more_than_the_player_alone():
    a = card(1, year=None, set_brand=None, card_number=None)
    b = card(2, year=None, set_brand=None, card_number=None)
    assert find_duplicates([a, b]) == []


# --- Scope --------------------------------------------------------------------

def test_card_backs_are_excluded():
    assert find_duplicates([card(1, side="back"), card(2, side="back")]) == []


def test_preview_cards_are_excluded():
    """Previews are not in the library yet."""
    assert find_duplicates([card(1, status="preview"), card(2, status="preview")]) == []


def test_normalisation_ignores_case_punctuation_and_hash():
    a = card(1, player="Barry Bonds", set_brand="Topps", card_number="#497")
    b = card(2, player="barry  bonds", set_brand="TOPPS", card_number="497")
    assert find_duplicates([a, b]) and find_duplicates([a, b])[0].tier == "certain"


def test_certain_groups_are_listed_before_possible_ones():
    cards = [
        card(1), card(2),                                     # certain pair
        card(3, player="Sammy Sosa", year="1997", set_brand="Upper Deck",
             card_number="189"),
        card(4, player="Sammy Sosa", year="1997", set_brand="Upper Deck",
             card_number=None),                               # possible pair
    ]
    groups = find_duplicates(cards)
    assert [g.tier for g in groups] == ["certain", "possible"]
