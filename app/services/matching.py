"""Score sold comps against a card identity and partition exact/near/graded.

A comp is judged by how many identity tokens (player, year, set, card number)
appear in its title. Exact matches drive the price; near matches are same-ish
cards (e.g. different grade); graded comps are tagged separately.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.ebay.base import SoldComp

# Keep in sync with pricecharting._GRADE_RE — a grader missing here is counted
# as a RAW sale, and slab prices are many times the raw price.
_GRADE_RE = re.compile(r"\b(psa|bgs|sgc|csg|cgc)\s*\d+(?:\.\d)?\b", re.IGNORECASE)


def _norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())


def _has_word(title_norm: str, word: str) -> bool:
    """Whole-word containment. Substring tests would match "bo" inside "bob"
    and the year "1989" inside "219890"."""
    return re.search(rf"\b{re.escape(word)}\b", title_norm) is not None


def _significant_words(token: str) -> list[str]:
    """Words worth matching on. Falls back to the whole token when every word is
    too short to be distinctive (e.g. a set named "SP")."""
    words = [w for w in token.split() if len(w) > 2]
    return words or ([token] if token else [])


@dataclass
class ScoredComp:
    comp: SoldComp
    match_type: str  # exact | near | graded | excluded
    match_reason: str


def _identity_tokens(card) -> dict[str, str]:
    """Map of field -> normalized token we expect to see in a matching title."""
    tokens: dict[str, str] = {}
    if getattr(card, "player", None):
        tokens["player"] = _norm(card.player).strip()
    if getattr(card, "year", None):
        tokens["year"] = _norm(card.year).strip()
    if getattr(card, "set_brand", None):
        tokens["set"] = _norm(card.set_brand).strip()
    if getattr(card, "card_number", None):
        tokens["number"] = _norm(str(card.card_number)).strip()
    if getattr(card, "parallel", None):
        tokens["parallel"] = _norm(card.parallel).strip()
    return tokens


def _player_tokens_present(title_norm: str, player_norm: str) -> bool:
    """All words of the player's name must appear in the title."""
    if not player_norm:
        return False
    return all(_has_word(title_norm, w) for w in player_norm.split() if w)


def score_comp(card, comp: SoldComp) -> ScoredComp:
    title_norm = _norm(comp.title)
    tokens = _identity_tokens(card)
    is_graded = bool(_GRADE_RE.search(comp.title or "")) or bool(
        comp.condition_grade and _GRADE_RE.search(comp.condition_grade)
    )

    player_ok = _player_tokens_present(title_norm, tokens.get("player", ""))
    if not player_ok:
        return ScoredComp(comp, "excluded", "player not found in title")

    matched = ["player"]
    if tokens.get("year") and _has_word(title_norm, tokens["year"]):
        matched.append("year")
    if tokens.get("set"):
        # Every significant word must appear: "Topps" alone is a different (and
        # differently priced) product from "Topps Chrome".
        if all(_has_word(title_norm, w) for w in _significant_words(tokens["set"])):
            matched.append("set")
    if tokens.get("number") and re.search(
        rf"#?\b{re.escape(tokens['number'])}\b", title_norm
    ):
        matched.append("number")

    reason = "matched: " + ", ".join(matched)

    if is_graded:
        return ScoredComp(comp, "graded", reason + " (graded)")

    # A parallel/insert sells for a multiple of its base card, so a sale can only
    # be an exact comp for one when the title names that parallel too.
    parallel_ok = True
    if tokens.get("parallel"):
        parallel_ok = all(
            _has_word(title_norm, w) for w in _significant_words(tokens["parallel"])
        )
        if parallel_ok:
            reason += ", parallel"

    # Exact requires player + at least two of year/set/number.
    strong = len(matched) - 1  # exclude the mandatory player
    if strong >= 2 and parallel_ok:
        return ScoredComp(comp, "exact", reason)
    if not parallel_ok:
        return ScoredComp(comp, "near", reason + " (parallel not in title)")
    if strong >= 1:
        return ScoredComp(comp, "near", reason)
    return ScoredComp(comp, "near", reason + " (weak)")


def partition(card, comps: list[SoldComp]) -> list[ScoredComp]:
    return [score_comp(card, c) for c in comps]
