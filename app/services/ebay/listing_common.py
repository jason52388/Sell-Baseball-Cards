"""Shared helpers for building eBay listing payloads from a Card.

eBay constraints encoded here (verified against eBay's 2026 listing docs):
  - TITLE      : 80 characters max
  - IMAGES     : 24 per listing max; URLs must be HTTPS for the Inventory API
  - DESCRIPTION: 500,000 characters max (HTML allowed)
  - ASPECT     : 65 chars per value, 30 values per aspect name
  - LOT CATEGORY: 261329 (Sports Trading Card Lots) with a "Number of Cards" aspect
"""
from __future__ import annotations

import hashlib
import html

MAX_TITLE = 80
MAX_IMAGES = 24
MAX_DESCRIPTION = 500_000
MAX_ASPECT_VALUE = 65
MAX_ASPECT_VALUES = 30

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


def card_image_url(card, base: str) -> str | None:
    """Public crop URL eBay can fetch, or None if we have nothing to show."""
    if card.crop_path and base:
        return f"{base.rstrip('/')}/crops/{card.id}.jpg"
    return None


# --- SET / LOT listings: combine N cards into a single eBay listing -----------


def _clean(value) -> str:
    return str(value).strip()


def _distinct(values) -> list[str]:
    """Order-preserving de-dupe of non-empty values."""
    seen: dict[str, None] = {}
    for v in values:
        c = _clean(v)
        if c and c.lower() != "none":
            seen.setdefault(c, None)
    return list(seen)


def build_set_title(cards) -> str:
    """A readable lot title, capped at eBay's 80-char limit.

    Leads with the card count + sport ("12-Card Baseball Card Lot"), then fills
    the remaining space with distinct sets and a few player names.
    """
    n = len(cards)
    sport = (_distinct(c.sport for c in cards) or ["Sports"])[0].title()
    title = f"{n}-Card {sport} Card Lot"
    sets = _distinct(f"{c.year or ''} {c.set_brand or ''}".strip() for c in cards)
    players = _distinct(c.player for c in cards)
    # Append optional segments only while they still fit within 80 chars.
    for seg in (", ".join(sets[:3]), "(" + ", ".join(players[:4]) + ")" if players else ""):
        if seg and len(title) + 3 + len(seg) <= MAX_TITLE:
            title = f"{title} - {seg}"
    return title[:MAX_TITLE]


def build_set_aspects(cards) -> dict[str, list[str]]:
    """Item specifics for a lot. 'Number of Cards' is expected for category 261329.
    Multi-value aspects (players/sets/seasons) are de-duped and capped to eBay's
    30-values / 65-chars-per-value limits."""
    def capped(values: list[str]) -> list[str]:
        return [v[:MAX_ASPECT_VALUE] for v in values[:MAX_ASPECT_VALUES]]

    aspects: dict[str, list[str]] = {"Number of Cards": [str(len(cards))]}
    sport = _distinct(c.sport for c in cards)
    if sport:
        aspects["Sport"] = capped([s.title() for s in sport])
    for name, vals in (
        ("Player/Athlete", _distinct(c.player for c in cards)),
        ("Set", _distinct(c.set_brand for c in cards)),
        ("Season", _distinct(c.year for c in cards)),
    ):
        if vals:
            aspects[name] = capped(vals)
    return aspects


def build_set_description(cards, *, shown_images: int | None = None) -> str:
    """HTML table describing every card in the lot (well under the 500K limit)."""
    n = len(cards)
    rows = []
    for i, c in enumerate(cards, 1):
        cells = [
            i,
            html.escape(_clean(c.year) or "—"),
            html.escape(_clean(c.set_brand) or "—"),
            html.escape(_clean(c.player) or "—"),
            html.escape(_clean(c.card_number) or "—"),
            html.escape(_clean(c.parallel) or "—"),
            html.escape(_clean(c.condition) or "—"),
        ]
        rows.append("<tr>" + "".join(f"<td>{v}</td>" for v in cells) + "</tr>")
    note = ""
    if shown_images is not None and shown_images < n:
        note = (
            f"<p><em>Photos show {shown_images} of {n} cards (eBay allows {MAX_IMAGES} "
            "images per listing); every card is listed in the table above.</em></p>"
        )
    desc = (
        f"<h2>{n}-card lot</h2>"
        f"<p>This listing is for the following {n} cards sold together as one lot:</p>"
        "<table border='1' cellpadding='4' cellspacing='0'>"
        "<tr><th>#</th><th>Year</th><th>Set</th><th>Player</th>"
        "<th>Card #</th><th>Parallel</th><th>Condition</th></tr>"
        + "".join(rows)
        + "</table>"
        + note
    )
    return desc[:MAX_DESCRIPTION]


def set_sku(cards) -> str:
    """Stable, unique, short (<50 char) SKU for a lot, derived from its card ids."""
    ids = sorted(c.id for c in cards)
    digest = hashlib.sha1(",".join(str(i) for i in ids).encode()).hexdigest()[:12]
    return f"SET-{ids[0]}-{len(ids)}-{digest}"


def set_image_urls(cards, base: str, limit: int = MAX_IMAGES) -> list[str]:
    """Combine each card's crop into one image list, capped at eBay's max (24)."""
    urls = []
    for c in cards:
        u = card_image_url(c, base)
        if u:
            urls.append(u)
        if len(urls) >= limit:
            break
    return urls
