"""Shared helpers for building eBay listing payloads from a Card."""
from __future__ import annotations

# Map our free-text condition / grade to eBay trading-card condition enums.
# Graded slabs use grading-specific enums; raw cards fall back to the default.
_CONDITION_MAP = {
    "mint": "USED_VERY_GOOD",
    "near-mint": "USED_VERY_GOOD",
    "near mint": "USED_VERY_GOOD",
    "excellent": "USED_GOOD",
    "very good": "USED_GOOD",
    "good": "USED_ACCEPTABLE",
    "poor": "USED_ACCEPTABLE",
}


def build_title(card) -> str:
    parts = [
        str(card.year or ""),
        card.set_brand or "",
        card.player or "",
        f"#{card.card_number}" if card.card_number else "",
        card.parallel or "",
    ]
    title = " ".join(p for p in parts if p).strip() or f"Baseball Card {card.id}"
    return title[:80]  # eBay title limit


def map_condition(card, default: str) -> str:
    cond = (card.condition or "").strip().lower()
    return _CONDITION_MAP.get(cond, default)


def build_aspects(card) -> dict[str, list[str]]:
    """Only include aspects that have real values — eBay rejects empty ones."""
    candidates = {
        "Player/Athlete": card.player,
        "Season": card.year,
        "Set": card.set_brand,
        "Card Number": card.card_number,
        "Parallel/Variety": card.parallel,
    }
    return {k: [str(v)] for k, v in candidates.items() if v not in (None, "", "None")}
