"""Find cards in the library that look like the same physical card.

Two things cause duplicates: the same card photographed twice across batches, and
genuinely owning two copies. Both are worth seeing, and the user decides which.

The rule is deliberately conservative, because a false positive costs more than a
miss here: a wrong grouping invites deleting a card you actually own.

  * Compare only fields BOTH cards carry a value for.
  * Any disagreement on a compared field means they are not duplicates. This is
    what keeps 2001 Topps Kerry Wood #786 and #623 apart.
  * Player, year, set and number all present and equal, with parallels agreeing
    (both absent counts as agreement) -> CERTAIN.
  * Agreeing on everything read, with something missing, and at least three
    fields actually compared -> POSSIBLE, labelled with what was missing.

Ambiguity is never resolved by guessing: a card whose number was not read sits
with a numbered card only when there is exactly one number in play.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Ordered so the label reads like a card: 2001 Topps Barry Bonds #497.
_IDENTITY = ("player", "year", "set_brand", "card_number", "parallel")


@dataclass
class DuplicateGroup:
    tier: str  # "certain" | "possible"
    label: str  # human-readable card identity
    reason: str  # why it is only "possible" ("" for certain)
    cards: list[Any] = field(default_factory=list)


def _norm(value: Any) -> str:
    """Lowercase, drop punctuation, collapse whitespace. '' means 'not read'."""
    if value is None:
        return ""
    text = re.sub(r"[^a-z0-9]+", " ", str(value).lower())
    return " ".join(text.split())


def _is_library_card(card: Any) -> bool:
    """Backs live on their front, and previews are not in the library yet."""
    if (getattr(card, "side", "front") or "front") == "back":
        return False
    return (getattr(card, "status", "") or "") != "preview"


def _label(card: Any) -> str:
    parts = [
        str(getattr(card, "year", "") or ""),
        getattr(card, "set_brand", "") or "",
        getattr(card, "player", "") or "",
        f"#{card.card_number}" if getattr(card, "card_number", None) else "",
        getattr(card, "parallel", "") or "",
    ]
    return " ".join(p for p in parts if p).strip() or "unidentified card"


def _certain_key(card: Any) -> tuple | None:
    """Full identity, or None when something needed for certainty is missing.

    Parallel is included as its normalised value, so two base cards (both blank)
    agree, and a base card never merges with a parallel.
    """
    values = [_norm(getattr(card, f, None)) for f in _IDENTITY]
    player, year, set_brand, number, parallel = values
    if not (player and year and set_brand and number):
        return None
    return (player, year, set_brand, number, parallel)


def _coarse_key(card: Any) -> tuple | None:
    """Identity without number or parallel. None when too thin to be meaningful."""
    player = _norm(getattr(card, "player", None))
    year = _norm(getattr(card, "year", None))
    set_brand = _norm(getattr(card, "set_brand", None))
    if not player or not (year or set_brand):
        return None
    return (player, year, set_brand)


def _missing_fields(cards: list[Any]) -> list[str]:
    """Which identity fields are absent on at least one of these cards."""
    names = {"card_number": "card number", "parallel": "parallel"}
    missing = []
    for attr, label in names.items():
        if any(not _norm(getattr(c, attr, None)) for c in cards):
            missing.append(label)
    return missing


def _possible_groups(cards: list[Any]) -> list[DuplicateGroup]:
    """Group cards that agree on everything read but have a gap somewhere."""
    buckets: dict[tuple, list[Any]] = {}
    for card in cards:
        key = _coarse_key(card)
        if key is not None:
            buckets.setdefault(key, []).append(card)

    groups: list[DuplicateGroup] = []
    for members in buckets.values():
        if len(members) < 2:
            continue
        numbers = {_norm(getattr(c, "card_number", None)) for c in members}
        parallels = {_norm(getattr(c, "parallel", None)) for c in members}
        # More than one actual value for a field = a real disagreement, or an
        # unreadable card that could belong to either. Both mean: say nothing.
        if len({n for n in numbers if n}) > 1 or len({p for p in parallels if p}) > 1:
            continue
        # Something must actually be missing, or this would have been certain.
        missing = _missing_fields(members)
        if not missing:
            continue
        # Require real evidence beyond the player's name.
        compared = sum(
            1
            for attr in _IDENTITY
            if all(_norm(getattr(c, attr, None)) for c in members)
        )
        if compared < 3:
            continue
        reference = max(members, key=lambda c: len(_label(c)))
        groups.append(
            DuplicateGroup(
                tier="possible",
                label=_label(reference),
                reason=f"{' and '.join(missing)} missing on one",
                cards=sorted(members, key=lambda c: c.id),
            )
        )
    return groups


def find_duplicates(cards: list[Any]) -> list[DuplicateGroup]:
    """Duplicate groups in the library, certain ones first."""
    library = [c for c in cards if _is_library_card(c)]

    certain_buckets: dict[tuple, list[Any]] = {}
    for card in library:
        key = _certain_key(card)
        if key is not None:
            certain_buckets.setdefault(key, []).append(card)

    certain: list[DuplicateGroup] = []
    settled: set[Any] = set()
    for members in certain_buckets.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda c: c.id)
        settled.update(c.id for c in members)
        certain.append(
            DuplicateGroup(
                tier="certain", label=_label(members[0]), reason="", cards=members
            )
        )

    remaining = [c for c in library if c.id not in settled]
    possible = _possible_groups(remaining)

    certain.sort(key=lambda g: g.label)
    possible.sort(key=lambda g: g.label)
    return certain + possible
