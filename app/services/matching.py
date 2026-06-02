"""Score sold comps against a card identity and partition exact/near/graded.

A comp is judged by how many identity tokens (player, year, set, card number)
appear in its title. Exact matches drive the price; near matches are same-ish
cards (e.g. different grade); graded comps are tagged separately.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.ebay.base import SoldComp

_GRADE_RE = re.compile(r"\b(psa|bgs|sgc)\s*\d+(?:\.\d)?\b", re.IGNORECASE)


def _norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())


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
    return tokens


def _player_tokens_present(title_norm: str, player_norm: str) -> bool:
    """All words of the player's name must appear in the title."""
    if not player_norm:
        return False
    return all(w in title_norm for w in player_norm.split() if w)


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
    if tokens.get("year") and tokens["year"] in title_norm:
        matched.append("year")
    if tokens.get("set"):
        # Match if any significant word of the set appears.
        set_words = [w for w in tokens["set"].split() if len(w) > 2]
        if any(w in title_norm for w in set_words):
            matched.append("set")
    if tokens.get("number") and re.search(
        rf"#?\b{re.escape(tokens['number'])}\b", title_norm
    ):
        matched.append("number")

    reason = "matched: " + ", ".join(matched)

    if is_graded:
        return ScoredComp(comp, "graded", reason + " (graded)")

    # Exact requires player + at least two of year/set/number.
    strong = len(matched) - 1  # exclude the mandatory player
    if "year" in matched and strong >= 2:
        return ScoredComp(comp, "exact", reason)
    if strong >= 1:
        return ScoredComp(comp, "near", reason)
    return ScoredComp(comp, "near", reason + " (weak)")


def partition(card, comps: list[SoldComp]) -> list[ScoredComp]:
    return [score_comp(card, c) for c in comps]
